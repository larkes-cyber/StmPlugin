import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .ai import LLMError
from .chats import ChatManager, drop_unanswered_tool_calls, strip_images
from .core import Logger

MAX_STEPS = 40
MAX_TOOL_RESULT_CHARS = 12000
IMAGE_PLACEHOLDER = "[снимок приложен следующим сообщением]"


class AgentBusy(Exception):
    """Исключение, если агент уже занят выполнением задачи в данном чате."""

    pass


def _image_part(image_url):
    return {"type": "image_url", "image_url": {"url": image_url}}


class AgentRunner(object):
    """
    Воркер диалогового цикла ReAct (Reasoning + Acting).
    Поддерживает пул воркеров (Worker Pool) для масштабирования параллельных чатов.
    """

    def __init__(self, config, registry, events, llm, system_prompt, chats=None, max_workers=2):
        self._config = config
        self._system_prompt = system_prompt
        self._registry = registry
        self._events = events
        self._llm = llm
        self._chats = chats if chats is not None else ChatManager(events)
        self._lock = threading.Lock()
        self._busy = False
        self._cancel = False

        # Заготовка под Worker Pool:
        self._pool = ThreadPoolExecutor(max_workers=max_workers)

    @property
    def busy(self):
        return self._busy

    def stop(self):
        Logger.log("i", "Запрошена остановка агента (busy=%s)", self._busy)
        if self._busy:
            self._cancel = True

    def submit(self, text, images=None, files=None, documents=None, chat_id=None):
        """Передает задачу в Worker Pool на исполнение."""
        images = images or []
        files = files or []
        documents = documents or []
        attachments = [{"kind": "image"} for _ in images] + files + documents

        with self._lock:
            if self._busy:
                raise AgentBusy(self._busy_message())
            chat = self._chats.begin_run(chat_id, self._title_hint(text, images, files, documents))
            self._busy = True
            self._cancel = False

        Logger.log(
            "i",
            "Сообщение пользователя [чат %s, сообщений ранее: %d]: %s | файлы=%s доки=%s фото=%d",
            chat.id,
            len(chat.messages),
            text,
            [f["name"] for f in files],
            [d["name"] for d in documents],
            len(images),
        )
        self._events.emit("user", {"text": text, "attachments": attachments}, chat_id=chat.id)

        # Отправляем задачу воркеру в пул потоков:
        future = self._pool.submit(self._run, chat, text, images, files, documents)
        return future

    def _run(self, chat, text, images, files, documents):
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
            Logger.log("i", "Системный промпт (%s) для чата %s", prompt_kind, chat.id)

            step = 0
            while step < MAX_STEPS:
                if self._cancel:
                    Logger.log("i", "Выполнение прервано на шаге %d", step)
                    self._events.emit("status", {"state": "idle"}, chat_id=chat.id)
                    break

                step += 1
                steps_done = step

                dialog = [system_msg] + messages
                drop_unanswered_tool_calls(dialog)

                self._events.emit("status", {"state": "thinking"}, chat_id=chat.id)
                msg_started_at = time.time()

                try:
                    assistant_msg, usage = self._llm.chat(dialog, tools=tools)
                except LLMError as err:
                    Logger.log("e", "Сбой LLM: %s", err)
                    self._events.emit("error", {"message": str(err)}, chat_id=chat.id)
                    break

                content = assistant_msg.get("content")
                tool_calls = assistant_msg.get("tool_calls") or []

                Logger.log(
                    "d",
                    "Шаг %d: LLM %.2fs, токены in/out=%s/%s, тулов=%d",
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
                    self._events.emit("assistant", {"text": content, "final": is_final}, chat_id=chat.id)

                if not tool_calls:
                    # Финальный ответ модели получен
                    break

                # Выполняем запрошенные инструменты
                for call in tool_calls:
                    if self._cancel:
                        break
                    call_id = call.get("id")
                    func = call.get("function") or {}
                    name = func.get("name")
                    try:
                        raw_args = func.get("arguments") or "{}"
                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except Exception:
                        args = {}

                    self._events.emit("tool_call", {"id": call_id, "name": name, "args": args}, chat_id=chat.id)
                    tool_outcome = self._registry.call(name, args, source="agent")
                    self._events.emit("tool_result", {"id": call_id, "name": name, "result": tool_outcome}, chat_id=chat.id)

                    result_text = json.dumps(tool_outcome, ensure_ascii=False, default=str)
                    if len(result_text) > MAX_TOOL_RESULT_CHARS:
                        result_text = result_text[:MAX_TOOL_RESULT_CHARS] + "… [обрезано]"

                    messages.append({"role": "tool", "tool_call_id": call_id, "content": result_text})
                    chat.messages = messages

                self._chats.checkpoint(chat)

        except Exception as e:
            Logger.logException("Сбой в работе воркера диалога")
            self._events.emit("error", {"message": "Внутренняя ошибка ассистента: %s" % e}, chat_id=chat.id)
        finally:
            elapsed = time.time() - started_at
            Logger.log("i", "Диалог в чате %s завершён: %d шагов, %.1fс", chat.id, steps_done, elapsed)
            strip_images(messages)
            chat.messages = messages
            try:
                self._chats.end_run(chat)
            except Exception:
                Logger.logException("Не удалось завершить сессию чата %s", chat.id)

            with self._lock:
                self._busy = False
            self._events.emit("status", {"state": "idle"}, chat_id=chat.id)

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

    def _busy_message(self):
        running = getattr(self._chats, "running_id", None)
        title = self._chats.title(running) if running else ""
        return "Ассистент ещё выполняет задачу в чате «%s» — дождитесь ответа или остановите его" % (title or "диалог")

    def _title_hint(self, text, images, files, documents):
        if text:
            return text
        for item in documents + files:
            if item.get("name"):
                return item["name"]
        return "Изображение" if images else ""


# Алиас для обратной совместимости
Agent = AgentRunner
