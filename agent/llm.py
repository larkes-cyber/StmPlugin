# -*- coding: utf-8 -*-
"""Клиент OpenAI-совместимого API (Chat Completions + tool calling) на стандартной библиотеке."""
import json
import ssl
import time
import urllib.error
import urllib.request


class LLMError(Exception):
    pass


class LLMClient(object):
    def __init__(self, config):
        self._config = config

    def _url(self, suffix):
        base = (self._config.get("base_url") or "").strip().rstrip("/")
        if not base:
            raise LLMError("Не задан URL LLM — откройте «Настройки» в чате")
        if base.endswith("/chat/completions"):
            base = base[:-len("/chat/completions")]
        return base + suffix

    def _ssl_context(self):
        ctx = ssl.create_default_context()
        if not self._config.get("verify_ssl"):
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _request(self, url, payload=None, timeout=180):
        api_key = self._config.get("api_key")
        if not api_key:
            raise LLMError("Не задан ключ LLM — откройте «Настройки» в чате")
        headers = {"Authorization": "Bearer " + api_key, "Accept": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(url, data=data, headers=headers)
        kwargs = {"timeout": timeout}
        if url.startswith("https"):
            kwargs["context"] = self._ssl_context()
        last_error = None
        for attempt in range(3):
            try:
                response = urllib.request.urlopen(request, **kwargs)
                return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")[:800]
                last_error = LLMError("LLM вернула HTTP %d: %s" % (e.code, body))
                if e.code not in (429, 500, 502, 503, 504):
                    raise last_error
            except urllib.error.URLError as e:
                last_error = LLMError("Нет соединения с LLM (%s): %s" % (url, e.reason))
            except (ValueError, OSError) as e:
                last_error = LLMError("Ошибка обращения к LLM: %s" % e)
            time.sleep(1.5 * (attempt + 1))
        raise last_error

    def chat(self, messages, tools=None):
        model = self._config.get("model")
        if not model:
            raise LLMError("Не задана модель LLM — откройте «Настройки» в чате")
        payload = {
            "model": model,
            "messages": messages,
            "temperature": self._config.get("temperature"),
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        body = self._request(self._url("/chat/completions"), payload)
        try:
            message = body["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise LLMError("Неожиданный ответ LLM: %s" % json.dumps(body, ensure_ascii=False)[:500])
        return message, body.get("usage") or {}

    def test(self):
        """Проверка подключения: короткий запрос без инструментов."""
        started = time.time()
        message, usage = self.chat([{"role": "user", "content": "Ответь одним словом: ok"}])
        return {"ok": True, "reply": (message.get("content") or "")[:200],
                "latency_s": round(time.time() - started, 2), "model": self._config.get("model")}
