import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .ai import LLMError
from .chats import ChatManager, drop_unanswered_tool_calls, strip_images
from .core import Logger, config, to_jsonable
from .core.dispatcher import (
    abort_chat_waits,
    abort_main_waits,
    bind_wait_chat,
    clear_chat_waits,
    clear_main_waits_abort,
)

MAX_STEPS = 40
MAX_TOOL_RESULT_CHARS = 12000


def _image_part(image_url):
    return {"type": "image_url", "image_url": {"url": image_url}}


def _attachment_labels(images, files, documents):
    """Подписи для ленты чата. Пути и текст документов сюда не попадают."""
    labels = [item["name"] for item in files]
    for document in documents:
        if document.get("truncated"):
            suffix = "обрезан"
        else:
            suffix = "%d симв." % document.get("chars", 0)
        labels.append("%s (%s)" % (document["name"], suffix))
    for number in range(1, len(images) + 1):
        labels.append("изображение %d" % number)
    return labels


class AgentRunner(object):
    """
    Цикл диалога в пуле потоков.

    Диалоги идут параллельно. Повторный запрос в тот же чат отклоняется,
    пока он не завершится. Вызовы в слайсер сериализует его собственный замок.
    """

    def __init__(self, config_obj, registry, events, llm, system_prompt, chats=None, review=None, slicer=None):
        self._config = config_obj or config
        self._system_prompt = system_prompt
        self._registry = registry
        self._events = events
        self._llm = llm
        self._review = review
        self._slicer = slicer
        self._chats = chats if chats is not None else ChatManager(events)

        self._lock = threading.Lock()
        self._active_chats = set()
        self._cancel_flags = set()
        self._futures = {}

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
                targets = [chat_id]
            else:
                Logger.log("i", "Запрошена остановка всех активных чатов: %s", self._active_chats)
                self._cancel_flags.update(self._active_chats)
                targets = list(self._active_chats)

            still_running = []
            already_idle = []
            for cid in targets:
                future = self._futures.get(cid)
                if future is not None and not future.done():
                    still_running.append(cid)
                elif cid in self._active_chats:
                    already_idle.append(cid)

        if self._review is not None:
            self._review.cancel(chat_id)
        if chat_id:
            abort_chat_waits(chat_id)
        elif still_running:
            abort_main_waits()
        for cid in already_idle:
            self._drop_run(cid)
        if not still_running:
            clear_main_waits_abort()

    def submit(self, text, images=None, files=None, documents=None, chat_id=None):
        """Отправляет задачу на исполнение воркеру из пула потоков."""
        images = images or []
        files = files or []
        documents = documents or []
        attachments = _attachment_labels(images, files, documents)

        chat = None
        try:
            with self._lock:
                target_chat = self._chats.resolve(chat_id)
                cid = target_chat.id

                if cid in self._active_chats:
                    title = self._chats.title(cid) or "диалог"
                    raise ValueError("В чате «%s» уже обрабатывается запрос — дождитесь ответа или остановите его" % title)

                chat = self._chats.begin_run(cid, self._title_hint(text, images, files, documents))
                self._active_chats.add(chat.id)
                self._cancel_flags.discard(chat.id)

            preview = " ".join((text or "").split())
            if len(preview) > 180:
                preview = preview[:179].rstrip() + "…"
            Logger.log(
                "i",
                "Запуск воркера [чат %s, история: %d, файлы=%d, доки=%d, фото=%d]: %s",
                chat.id,
                len(chat.messages),
                len(files),
                len(documents),
                len(images),
                preview,
            )
            self._events.emit("user", {"text": text, "attachments": attachments}, chat_id=chat.id)
            future = self._pool.submit(self._run_task, chat, text, images, files, documents)
            with self._lock:
                self._futures[chat.id] = future
            return chat.id
        except Exception:
            if chat is not None:
                self._drop_run(chat.id)
            raise

    def _is_cancelled(self, chat_id):
        with self._lock:
            return chat_id in self._cancel_flags

    def _run_task(self, chat, text, images, files, documents):
        chat_id = chat.id
        bind_wait_chat(chat_id)
        started_at = time.time()
        steps_done = 0
        messages = list(chat.messages)

        try:
            # Изоляция стола: синхронизируем сцену и параметры под конкретный чат
            if self._slicer and hasattr(self._slicer, "sync_for_chat"):
                self._slicer.sync_for_chat(chat)

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

                    # Гарантируем, что перед вызовом инструмента сцена принадлежит этому чату:
                    if self._slicer and hasattr(self._slicer, "sync_for_chat"):
                        self._slicer.sync_for_chat(chat)

                    tool_outcome = self._registry.call(
                        name, args, source="agent", call_id=call_id, chat_id=chat_id
                    )

                    # Автоматически обновляем снимок параметров в паспорте чата
                    if tool_outcome and tool_outcome.get("ok"):
                        if name == "load_model" and args.get("path"):
                            chat.update_slicer_state({"model_path": args["path"]})
                        elif name == "set_settings" and args.get("changes"):
                            chat.update_slicer_state({"settings": args["changes"]})
                        elif name == "set_material" and (args.get("material_id") or args.get("id")):
                            chat.update_slicer_state({"material": args.get("material_id") or args.get("id")})
                        elif name == "set_print_mode" and args.get("mode"):
                            chat.update_slicer_state({"print_mode": args["mode"]})

                    result_text = json.dumps(to_jsonable(tool_outcome), ensure_ascii=False)
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

            clear_chat_waits(chat_id)
            with self._lock:
                self._active_chats.discard(chat_id)
                self._cancel_flags.discard(chat_id)
                self._futures.pop(chat_id, None)
                others_running = bool(self._active_chats)

            if not others_running:
                clear_main_waits_abort()
            self._events.emit("status", {"state": "idle"}, chat_id=chat_id)

    def _drop_run(self, chat_id):
        """Снимает флаг запуска, если воркер так и не начал работать."""
        with self._lock:
            self._active_chats.discard(chat_id)
            self._cancel_flags.discard(chat_id)
            self._futures.pop(chat_id, None)
        try:
            chat = self._chats.resolve(chat_id)
            self._chats.end_run(chat)
        except Exception:
            Logger.logException("Не удалось снять запуск чата %s", chat_id)
        self._events.emit("status", {"state": "idle"}, chat_id=chat_id)

    def _user_message(self, text, images, files, documents):
        parts = []
        if text:
            parts.append(text)
        elif documents:
            parts.append("Задача описана в приложенном документе.")

        for doc in documents:
            note = ""
            if doc.get("truncated"):
                note = " (текст обрезан до %d символов)" % doc.get("chars", 0)
            parts.append("\n=== Документ: %s%s ===\n%s\n" % (doc["name"], note, doc["text"]))

        if files:
            parts.append("\nПрикреплённые модели. Загрузи каждую через load_model, аргумент path — абсолютный путь:")
            for item in files:
                parts.append("- %s: %s" % (item["name"], item["path"]))

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
