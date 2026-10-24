import json
import threading
import time

from .chats import ChatManager, drop_unanswered_tool_calls, strip_images
from .llm import LLMError
from .log import log, log_exception, shorten

MAX_STEPS = 40
MAX_TOOL_RESULT_CHARS = 12000
IMAGE_PLACEHOLDER = "[снимок приложен следующим сообщением]"


class AgentBusy(Exception):
    pass


def _image_part(image_url):
    return {"type": "image_url", "image_url": {"url": image_url}}


class Agent(object):
    def __init__(self, config, registry, events, llm, system_prompt, chats=None):
        self._config = config
        self._system_prompt = system_prompt
        self._registry = registry
        self._events = events
        self._llm = llm
        self._chats = chats if chats is not None else ChatManager(events)
        self._messages = []
        self._lock = threading.Lock()
        self._busy = False
        self._cancel = False

    @property
    def busy(self):
        return self._busy

    def stop(self):
        log("i", "stop requested (busy=%s)", self._busy)
        if self._busy:
            self._cancel = True

    def submit(self, text, images=None, files=None, documents=None, chat_id=None):
        images = images or []
        files = files or []
        documents = documents or []
        attachments = self._attachment_labels(images, files, documents)

        with self._lock:
            if self._busy:
                raise AgentBusy(self._busy_message())
            chat, note = self._chats.begin_run(chat_id, self._title_hint(text, images, files, documents))
            self._messages = chat.messages
            self._busy = True
            self._cancel = False

        log(
            "i",
            "user message [chat %s, %d messages before%s]: %s | files=%s docs=%s images=%d",
            chat.id,
            len(chat.messages),
            ", shared scene note" if note else "",
            shorten(text, 300),
            [file["name"] for file in files],
            [document["name"] for document in documents],
            len(images),
        )
        self._events.emit("user", text=text, attachments=attachments)

        thread = threading.Thread(
            target=self._run,
            args=(chat, text, images, files, documents, note),
            name="AIAssistantAgent",
        )
        thread.daemon = True
        thread.start()
        return thread

    def _busy_message(self):
        running_id = self._chats.running_id
        if running_id is None:
            return "Агент ещё работает над предыдущим запросом"
        try:
            title = self._chats.title(running_id)
        except ValueError:
            return "Агент ещё работает над предыдущим запросом"
        return "Агент ещё работает в чате «%s» — дождитесь ответа или остановите его" % title

    @staticmethod
    def _title_hint(text, images, files, documents):
        for line in (text or "").splitlines():
            if line.strip():
                return line
        names = [document["name"] for document in documents] + [file["name"] for file in files]
        if names:
            return ", ".join(names)
        return "Изображение" if images else ""

    @staticmethod
    def _attachment_labels(images, files, documents):
        labels = [file["name"] for file in files]
        for document in documents:
            labels.append("%s (%d симв.)" % (document["name"], document["chars"]))
        for number in range(1, len(images) + 1):
            labels.append("изображение %d" % number)
        return labels

    def _user_message(self, text, images, files, documents, note=None):
        parts = []
        if note:
            parts.append(note + "\n")
        if text:
            parts.append(text)
        elif documents:
            parts.append("Задача описана в приложенном документе.")

        for document in documents:
            truncated_note = ""
            if document.get("truncated"):
                truncated_note = " (обрезан до %d симв.)" % document["chars"]
            parts.append(
                "\nПриложенный документ «%s»%s:\n<<<\n%s\n>>>"
                % (document["name"], truncated_note, document["text"])
            )

        if files:
            parts.append("\nПрикреплённые файлы моделей (используй путь в load_model):")
            for file in files:
                parts.append("- %s: %s" % (file["name"], file["path"]))

        if images:
            parts.append("\nПрикреплено изображений: %d." % len(images))

        full_text = "\n".join(parts).strip()
        if not images:
            return {"role": "user", "content": full_text}

        content = [{"type": "text", "text": full_text}]
        for image_url in images:
            content.append(_image_part(image_url))
        return {"role": "user", "content": content}

    def _run(self, chat, text, images, files, documents, note=None):
        started_at = time.time()
        steps_done = 0
        try:
            self._messages.append(self._user_message(text, images, files, documents, note))
            tools = self._registry.openai_tools()
            prompt_kind = "custom" if self._system_prompt.is_custom() else "default"
            log("i", "system prompt: %s", prompt_kind)

            for step in range(1, MAX_STEPS + 1):
                steps_done = step
                if self._cancel:
                    log("i", "run cancelled at step %d", step)
                    self._events.emit("assistant", text="Остановлено пользователем.")
                    return
                finished = self._step(step, tools)
                self._chats.checkpoint(chat)
                if finished:
                    return

            log("w", "step limit %d reached", MAX_STEPS)
            self._events.emit("assistant", text="Достигнут лимит шагов агента — уточните задачу или продолжите.")
        except LLMError as error:
            log("e", "LLM error: %s", error)
            self._events.emit("error", message=str(error))
            drop_unanswered_tool_calls(self._messages)
        except Exception as error:
            log_exception("Agent failed")
            self._events.emit("error", message="Внутренняя ошибка агента: %s" % error)
            drop_unanswered_tool_calls(self._messages)
        finally:
            log("i", "run finished in chat %s: %d steps, %.1fs", chat.id, steps_done, time.time() - started_at)
            strip_images(self._messages)
            try:
                self._chats.end_run(chat)
            except Exception:
                log_exception("chat %s: failed to finish run" % chat.id)
            self._busy = False
            self._events.emit("status", state="idle", chat_id=chat.id)

    def _step(self, step, tools):
        self._events.emit("status", state="thinking")
        started_at = time.time()

        history = [{"role": "system", "content": self._system_prompt.text()}] + self._messages
        message, usage = self._llm.chat(history, tools)
        content = message.get("content") or ""
        tool_calls = message.get("tool_calls") or []

        tool_names = [(call.get("function") or {}).get("name") for call in tool_calls]
        log(
            "d",
            "step %d: LLM %.2fs, tokens in/out=%s/%s, tool_calls=%s, text=%s",
            step,
            time.time() - started_at,
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            tool_names,
            shorten(content, 200),
        )

        assistant_message = {"role": "assistant", "content": content or None}
        if tool_calls:
            assistant_message["tool_calls"] = tool_calls
        self._messages.append(assistant_message)

        if content:
            self._events.emit("assistant", text=content, final=not tool_calls)
        if not tool_calls:
            return True

        self._execute_tool_calls(tool_calls)
        return False

    def _execute_tool_calls(self, tool_calls):
        images = []
        for call in tool_calls:
            name = (call.get("function") or {}).get("name")
            if self._cancel:
                result = {"ok": False, "error": "Отменено пользователем"}
            else:
                result = self._call_tool(call, name)

            value = result.get("result")
            if result.get("ok") and isinstance(value, dict) and "_image" in value:
                images.append((name, value["_image"]))
                result = dict(result, result=dict(value, _image=IMAGE_PLACEHOLDER))

            self._messages.append({
                "role": "tool",
                "tool_call_id": call.get("id"),
                "content": self._tool_message_content(result),
            })

        if images:
            self._messages.append(self._images_message(images))

    def _call_tool(self, call, name):
        self._events.emit("status", state="tool", tool=name)
        raw_arguments = (call.get("function") or {}).get("arguments") or "{}"
        try:
            arguments = json.loads(raw_arguments)
        except ValueError:
            log("w", "invalid JSON arguments for %s: %s", name, shorten(raw_arguments, 300))
            return {"ok": False, "error": "Аргументы не являются корректным JSON"}
        return self._registry.call(name, arguments, source="agent", call_id=call.get("id"))

    @staticmethod
    def _tool_message_content(result):
        payload = json.dumps(result, ensure_ascii=False, default=str)
        if len(payload) > MAX_TOOL_RESULT_CHARS:
            payload = payload[:MAX_TOOL_RESULT_CHARS] + "…(обрезано)"
        return payload

    @staticmethod
    def _images_message(images):
        tool_names = ", ".join(name for name, _ in images)
        content = [{
            "type": "text",
            "text": "Изображения, полученные инструментами (%s). Оцени их и продолжай работу." % tool_names,
        }]
        for _, image_url in images:
            content.append(_image_part(image_url))
        return {"role": "user", "content": content}
