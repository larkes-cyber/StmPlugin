# -*- coding: utf-8 -*-
"""Агент: цикл диалога с LLM и вызова инструментов. Работает в отдельном потоке."""
import json
import threading

from .llm import LLMError
from .log import log_exception
from .prompts import SYSTEM_PROMPT

MAX_STEPS = 40
MAX_TOOL_RESULT_CHARS = 12000


class AgentBusy(Exception):
    pass


class Agent(object):
    def __init__(self, config, registry, events, llm):
        self._config = config
        self._registry = registry
        self._events = events
        self._llm = llm
        self._messages = []
        self._lock = threading.Lock()
        self._busy = False
        self._cancel = False

    @property
    def busy(self):
        return self._busy

    def reset(self):
        if self._busy:
            self._cancel = True
        self._messages = []
        self._events.clear()

    def stop(self):
        if self._busy:
            self._cancel = True

    def submit(self, text, images=None, files=None):
        """images: список data:URL; files: список {"name", "path"} сохранённых моделей."""
        with self._lock:
            if self._busy:
                raise AgentBusy("Агент ещё работает над предыдущим запросом")
            self._busy = True
            self._cancel = False
        self._events.emit("user", text=text,
                          attachments=[f["name"] for f in (files or [])] + ["изображение %d" % (i + 1) for i in range(len(images or []))])
        thread = threading.Thread(target=self._run, args=(text, images or [], files or []), name="AIAssistantAgent")
        thread.daemon = True
        thread.start()
        return thread

    # ------------------------------------------------------------------
    def _user_message(self, text, images, files):
        parts = [text or ""]
        if files:
            parts.append("\nПрикреплённые файлы моделей (используй путь в load_model):")
            for f in files:
                parts.append("- %s: %s" % (f["name"], f["path"]))
        if images:
            parts.append("\nПрикреплено изображений: %d." % len(images))
        full_text = "\n".join(parts).strip()
        if not images:
            return {"role": "user", "content": full_text}
        content = [{"type": "text", "text": full_text}]
        for url in images:
            content.append({"type": "image_url", "image_url": {"url": url}})
        return {"role": "user", "content": content}

    def _strip_old_images(self):
        """Картинки уже проанализированы — в истории оставляем только пометку, чтобы не тратить токены."""
        for message in self._messages:
            if message.get("role") == "user" and isinstance(message.get("content"), list):
                texts = [p.get("text", "") for p in message["content"] if p.get("type") == "text"]
                count = len([p for p in message["content"] if p.get("type") == "image_url"])
                message["content"] = "\n".join(texts) + "\n[%d изображ. было приложено ранее]" % count

    def _run(self, text, images, files):
        try:
            self._messages.append(self._user_message(text, images, files))
            tools = self._registry.openai_tools()
            for _ in range(MAX_STEPS):
                if self._cancel:
                    self._events.emit("assistant", text="Остановлено пользователем.")
                    break
                self._events.emit("status", state="thinking")
                message, usage = self._llm.chat([{"role": "system", "content": SYSTEM_PROMPT}] + self._messages, tools)
                content = message.get("content") or ""
                tool_calls = message.get("tool_calls") or []
                entry = {"role": "assistant", "content": content if content else None}
                if tool_calls:
                    entry["tool_calls"] = tool_calls
                self._messages.append(entry)
                if content:
                    self._events.emit("assistant", text=content, final=not tool_calls)
                if not tool_calls:
                    break
                for call in tool_calls:
                    if self._cancel:
                        result = {"ok": False, "error": "Отменено пользователем"}
                    else:
                        function = call.get("function") or {}
                        name = function.get("name")
                        self._events.emit("status", state="tool", tool=name)
                        try:
                            args = json.loads(function.get("arguments") or "{}")
                        except ValueError:
                            args = None
                        if args is None:
                            result = {"ok": False, "error": "Аргументы не являются корректным JSON"}
                        else:
                            result = self._registry.call(name, args, source="agent", call_id=call.get("id"))
                    payload = json.dumps(result, ensure_ascii=False, default=str)
                    if len(payload) > MAX_TOOL_RESULT_CHARS:
                        payload = payload[:MAX_TOOL_RESULT_CHARS] + "…(обрезано)"
                    self._messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": payload})
            else:
                self._events.emit("assistant", text="Достигнут лимит шагов агента — уточните задачу или продолжите.")
        except LLMError as e:
            self._events.emit("error", message=str(e))
            self._rollback_dangling()
        except Exception as e:
            log_exception("Agent failed")
            self._events.emit("error", message="Внутренняя ошибка агента: %s" % e)
            self._rollback_dangling()
        finally:
            self._strip_old_images()
            self._busy = False
            self._events.emit("status", state="idle")

    def _rollback_dangling(self):
        """Не оставлять в истории assistant с tool_calls без ответов инструментов — API такое не примет."""
        while self._messages:
            last = self._messages[-1]
            if last.get("role") == "assistant" and last.get("tool_calls"):
                self._messages.pop()
                continue
            if last.get("role") == "tool":
                answered = set()
                idx = len(self._messages) - 1
                while idx >= 0 and self._messages[idx].get("role") == "tool":
                    answered.add(self._messages[idx].get("tool_call_id"))
                    idx -= 1
                if idx >= 0 and self._messages[idx].get("tool_calls"):
                    expected = set(c.get("id") for c in self._messages[idx]["tool_calls"])
                    if not expected.issubset(answered):
                        del self._messages[idx:]
                        continue
            break
