# -*- coding: utf-8 -*-
"""Агент: цикл диалога с LLM и вызова инструментов. Работает в отдельном потоке."""
import json
import threading
import time

from .llm import LLMError
from .log import log, log_exception, short
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
        log("i", "dialog reset (busy=%s, %d messages dropped)", self._busy, len(self._messages))
        if self._busy:
            self._cancel = True
        self._messages = []
        self._events.clear()

    def stop(self):
        log("i", "stop requested (busy=%s)", self._busy)
        if self._busy:
            self._cancel = True

    def submit(self, text, images=None, files=None, documents=None):
        """images: data:URL; files: {"name", "path"} сохранённых моделей; documents: {"name", "text", ...}."""
        with self._lock:
            if self._busy:
                raise AgentBusy("Агент ещё работает над предыдущим запросом")
            self._busy = True
            self._cancel = False
        images, files, documents = images or [], files or [], documents or []
        log("i", "user message: %s | files=%s docs=%s images=%d", short(text, 300),
            [f["name"] for f in files], [d["name"] for d in documents], len(images))
        attachments = [f["name"] for f in files] + \
            ["%s (%d симв.)" % (d["name"], d["chars"]) for d in documents] + \
            ["изображение %d" % (i + 1) for i in range(len(images))]
        self._events.emit("user", text=text, attachments=attachments)
        thread = threading.Thread(target=self._run, args=(text, images, files, documents), name="AIAssistantAgent")
        thread.daemon = True
        thread.start()
        return thread

    # ------------------------------------------------------------------
    def _user_message(self, text, images, files, documents=None):
        parts = [text or ("Задача описана в приложенном документе." if documents else "")]
        for doc in documents or []:
            parts.append("\nПриложенный документ «%s»%s:\n<<<\n%s\n>>>" % (
                doc["name"], " (обрезан до %d симв.)" % doc["chars"] if doc.get("truncated") else "", doc["text"]))
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

    def _run(self, text, images, files, documents=None):
        run_started = time.time()
        steps = 0
        try:
            self._messages.append(self._user_message(text, images, files, documents))
            tools = self._registry.openai_tools()
            for step in range(MAX_STEPS):
                steps = step + 1
                if self._cancel:
                    log("i", "run cancelled at step %d", steps)
                    self._events.emit("assistant", text="Остановлено пользователем.")
                    break
                self._events.emit("status", state="thinking")
                started = time.time()
                message, usage = self._llm.chat([{"role": "system", "content": SYSTEM_PROMPT}] + self._messages, tools)
                content = message.get("content") or ""
                tool_calls = message.get("tool_calls") or []
                log("d", "step %d: LLM %.2fs, tokens in/out=%s/%s, tool_calls=%s, text=%s",
                    steps, time.time() - started, usage.get("prompt_tokens"), usage.get("completion_tokens"),
                    [(c.get("function") or {}).get("name") for c in tool_calls], short(content, 200))
                entry = {"role": "assistant", "content": content if content else None}
                if tool_calls:
                    entry["tool_calls"] = tool_calls
                self._messages.append(entry)
                if content:
                    self._events.emit("assistant", text=content, final=not tool_calls)
                if not tool_calls:
                    break
                step_images = []
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
                            log("w", "invalid JSON arguments for %s: %s", name, short(function.get("arguments"), 300))
                            result = {"ok": False, "error": "Аргументы не являются корректным JSON"}
                        else:
                            result = self._registry.call(name, args, source="agent", call_id=call.get("id"))
                    value = result.get("result")
                    if result.get("ok") and isinstance(value, dict) and "_image" in value:
                        # tool-сообщения в Chat Completions — только текст: картинку отдаём следующим
                        # user-сообщением, после ответов на все вызовы этого шага
                        step_images.append((function.get("name"), value["_image"]))
                        result = dict(result, result=dict(value, _image="[снимок приложен следующим сообщением]"))
                    payload = json.dumps(result, ensure_ascii=False, default=str)
                    if len(payload) > MAX_TOOL_RESULT_CHARS:
                        payload = payload[:MAX_TOOL_RESULT_CHARS] + "…(обрезано)"
                    self._messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": payload})
                if step_images:
                    content = [{"type": "text", "text": "Изображения, полученные инструментами (%s). "
                                "Оцени их и продолжай работу." % ", ".join(n for n, _ in step_images)}]
                    content += [{"type": "image_url", "image_url": {"url": url}} for _, url in step_images]
                    self._messages.append({"role": "user", "content": content})
            else:
                log("w", "step limit %d reached", MAX_STEPS)
                self._events.emit("assistant", text="Достигнут лимит шагов агента — уточните задачу или продолжите.")
        except LLMError as e:
            log("e", "LLM error: %s", e)
            self._events.emit("error", message=str(e))
            self._rollback_dangling()
        except Exception as e:
            log_exception("Agent failed")
            self._events.emit("error", message="Внутренняя ошибка агента: %s" % e)
            self._rollback_dangling()
        finally:
            log("i", "run finished: %d steps, %.1fs", steps, time.time() - run_started)
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
