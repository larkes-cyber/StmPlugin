import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .ai import LLMError
from .chats import ChatManager, drop_unanswered_tool_calls, strip_images
from .core import Logger, config

MAX_STEPS = 40
MAX_TOOL_RESULT_CHARS = 12000


def _image_part(image_url):
    return {"type": "image_url", "image_url": {"url": image_url}}


class AgentRunner(object):
    """
    Многопоточный раннер диалогов на базе Worker Pool (ThreadPoolExecutor).
    Обеспечивает параллельную обработку сообщений в разных чатах без взаимных блокировок.
    """

    def __init__(self, config_obj, registry, events, llm, system_prompt, chats=None):
        self._config = config_obj or config
        self._system_prompt = system_prompt
        self._registry = registry
        self._events = events
        self._llm = llm
        self._chats = chats if chats is not None else ChatManager(events)

        self._lock = threading.Lock()
        self._active_chats = set()
        self._cancel_flags = set()

        max_workers = int(self._config.get("max_workers", 4))
        self._pool = ThreadPoolExecutor(max_workers=max_workers)

    @property
    def busy(self):
        with self._lock:
            return bool(self._active_chats)

    def is_chat_busy(self, chat_id):
        with self._lock:
            return chat_id in self._active_chats

    def stop(self, chat_id=None):
        """Останавливает выполнение конкретного чата или всех активных диалогов."""
        with self._lock:
            if chat_id:
                Logger.log("i", "Запрошена остановка чата %s", chat_id)
                self._cancel_flags.add(chat_id)
            else:
                Logger.log("i", "Запрошена остановка всех активных чатов: %s", self._active_chats)
                self._cancel_flags.update(self._active_chats)

    def submit(self, text, images=None, files=None, documents=None, chat_id=None):
        """Отправляет задачу на исполнение воркеру из пула потоков."""
        images = images or []
        files = files or []
        documents = documents or []
        attachments = [{"kind": "image"} for _ in images] + files + documents

        with self._lock:
            target_chat = self._chats.resolve(chat_id)
            cid = target_chat.id

            if cid in self._active_chats:
                title = self._chats.title(cid) or "диалог"
                raise ValueError("В чате «%s» уже обрабатывается запрос — дождитесь ответа или остановите его" % title)

            chat = self._chats.begin_run(cid, self._title_hint(text, images, files, documents))
            self._active_chats.add(chat.id)
            self._cancel_flags.discard(chat.id)

        Logger.log(
            "i",
            "Запуск воркера [чат %s, история: %d]: %s | файлы=%s доки=%s фото=%d",
            chat.id,
            len(chat.messages),
            text,
            [f["name"] for f in files],
            [d["name"] for d in documents],
            len(images),
        )
        self._events.emit("user", {"text": text, "attachments": attachments}, chat_id=chat.id)

        # Отправляем задачу в Worker Pool:
        future = self._pool.submit(self._run_task, chat, text, images, files, documents)
        return future

    def _is_cancelled(self, chat_id):
        with self._lock:
            return chat_id in self._cancel_flags

    def _run_task(self, chat, text, images, files, documents):
        chat_id = chat.id
        started_at = time.time()
        steps_done = 0
        messages = list(chat.messages)

        try:
            user_msg = self._user_message(text, images, files, documents)
            messages.append(user_msg)
            chat.messages = messages

            tools = self._registry.openai_tools()
            prompt_kind = "custom" if self._system_prompt.is_custom() else "default"
            system_msg = {"role": "system", "content": self._system_prompt.text()}
            Logger.log("i", "Воркер взял задачу для чата %s (промпт: %s)", chat_id, prompt_kind)

            step = 0
            while step < MAX_STEPS:
                if self._is_cancelled(chat_id):
                    Logger.log("i", "Чат %s отменен на шаге %d", chat_id, step)
                    self._events.emit("status", {"state": "idle"}, chat_id=chat_id)
                    break

                step += 1
                steps_done = step

                dialog = [system_msg] + messages
                drop_unanswered_tool_calls(dialog)

                self._events.emit("status", {"state": "thinking"}, chat_id=chat_id)
                msg_started_at = time.time()

                try:
                    assistant_msg, usage = self._llm.chat(dialog, tools=tools)
                except LLMError as err:
                    Logger.log("e", "Сбой LLM в чате %s: %s", chat_id, err)
                    self._events.emit("error", {"message": str(err)}, chat_id=chat_id)
                    break

                content = assistant_msg.get("content")
                tool_calls = assistant_msg.get("tool_calls") or []

                Logger.log(
                    "d",
                    "Чат %s, шаг %d: LLM %.2fs, токены in/out=%s/%s, тулов=%d",
                    chat_id,
                    step,
                    time.time() - msg_started_at,
                    usage.get("prompt_tokens"),
                    usage.get("completion_tokens"),
                    len(tool_calls),
                )

                messages.append(assistant_msg)
                chat.messages = messages

                if content:
                    is_final = not tool_calls
                    self._events.emit("assistant", {"text": content, "final": is_final}, chat_id=chat_id)

                if not tool_calls:
                    break

                for call in tool_calls:
                    if self._is_cancelled(chat_id):
                        break
                    call_id = call.get("id")
                    func = call.get("function") or {}
                    name = func.get("name")
                    try:
                        raw_args = func.get("arguments") or "{}"
                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except Exception:
                        args = {}

                    self._events.emit("tool_call", {"id": call_id, "name": name, "args": args}, chat_id=chat_id)
                    tool_outcome = self._registry.call(name, args, source="agent")
                    self._events.emit("tool_result", {"id": call_id, "name": name, "result": tool_outcome}, chat_id=chat_id)

                    result_text = json.dumps(tool_outcome, ensure_ascii=False, default=str)
                    if len(result_text) > MAX_TOOL_RESULT_CHARS:
                        result_text = result_text[:MAX_TOOL_RESULT_CHARS] + "… [обрезано]"

                    messages.append({"role": "tool", "tool_call_id": call_id, "content": result_text})
                    chat.messages = messages

                self._chats.checkpoint(chat)

        except Exception as e:
            Logger.logException("Сбой в воркере диалога чата %s", chat_id)
            self._events.emit("error", {"message": "Внутренняя ошибка ассистента: %s" % e}, chat_id=chat_id)
        finally:
            elapsed = time.time() - started_at
            Logger.log("i", "Воркер завершил чат %s: %d шагов, %.1fс", chat_id, steps_done, elapsed)
            strip_images(messages)
            chat.messages = messages
            try:
                self._chats.end_run(chat)
            except Exception:
                Logger.logException("Не удалось сохранить сессию чата %s", chat_id)

            with self._lock:
                self._active_chats.discard(chat_id)
                self._cancel_flags.discard(chat_id)

            self._events.emit("status", {"state": "idle"}, chat_id=chat_id)

    def _user_message(self, text, images, files, documents):
        parts = []
        if text:
            parts.append(text)
        elif documents:
            parts.append("Задача описана в приложенном документе.")

        for doc in documents:
            parts.append("\n=== Документ: %s ===\n%s\n" % (doc["name"], doc["text"]))

        if files:
            file_names = ", ".join(f["name"] for f in files)
            parts.append("\nПрикреплены 3D-файлы: %s (загрузите их на стол через load_model)" % file_names)

        full_text = "\n".join(parts)
        if not images:
            return {"role": "user", "content": full_text}

        content_parts = [{"type": "text", "text": full_text}]
        for img_url in images:
            content_parts.append(_image_part(img_url))
        return {"role": "user", "content": content_parts}

    def _title_hint(self, text, images, files, documents):
        if text:
            return text
        for item in documents + files:
            if item.get("name"):
                return item["name"]
        return "Изображение" if images else ""
