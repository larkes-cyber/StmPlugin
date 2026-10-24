import json
import ssl
import time
import urllib.error
import urllib.request

from .core import Logger

CHAT_PATH = "/chat/completions"
REQUEST_TIMEOUT = 180
MAX_ATTEMPTS = 3
RETRY_STATUS_CODES = (429, 500, 502, 503, 504)


class LLMError(Exception):
    pass


class LLMClient(object):
    def __init__(self, config):
        self._config = config

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

        body = self._request(self._endpoint(CHAT_PATH), payload)

        try:
            message = body["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            preview = json.dumps(body, ensure_ascii=False)[:500]
            raise LLMError("Неожиданный ответ LLM: %s" % preview)

        usage = body.get("usage") or {}
        return message, usage

    def test(self):
        started_at = time.time()
        message, _ = self.chat([{"role": "user", "content": "Ответь одним словом: ok"}])
        reply = message.get("content") or ""
        return {
            "ok": True,
            "reply": reply[:200],
            "latency_s": round(time.time() - started_at, 2),
            "model": self._config.get("model"),
        }

    def _endpoint(self, path):
        base_url = (self._config.get("base_url") or "").strip().rstrip("/")
        if not base_url:
            raise LLMError("Не задан URL LLM — откройте «Настройки» в чате")

        if base_url.endswith(CHAT_PATH):
            base_url = base_url[:-len(CHAT_PATH)]
        return base_url + path

    def _ssl_context(self):
        context = ssl.create_default_context()
        if not self._config.get("verify_ssl"):
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    def _request(self, url, payload):
        api_key = self._config.get("api_key")
        if not api_key:
            raise LLMError("Не задан ключ LLM — откройте «Настройки» в чате")

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": "Bearer " + api_key,
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
        }
        request = urllib.request.Request(url, data=data, headers=headers)

        options = {"timeout": REQUEST_TIMEOUT}
        if url.startswith("https"):
            options["context"] = self._ssl_context()

        last_error = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return self._send(request, options, attempt)
            except urllib.error.HTTPError as error:
                body = error.read().decode("utf-8", "replace")[:800]
                Logger.log("w", "LLM HTTP %d from %s: %s", error.code, url, body[:300])
                last_error = LLMError("LLM вернула HTTP %d: %s" % (error.code, body))
                if error.code not in RETRY_STATUS_CODES:
                    raise last_error
            except urllib.error.URLError as error:
                Logger.log("w", "LLM connection error %s: %s", url, error.reason)
                last_error = LLMError("Нет соединения с LLM (%s): %s" % (url, error.reason))
            except OSError as error:
                Logger.log("w", "LLM error %s: %s", url, error)
                last_error = LLMError("Ошибка обращения к LLM: %s" % error)

            time.sleep(1.5 * attempt)

        raise last_error

    def _send(self, request, options, attempt):
        started_at = time.time()
        Logger.log("d", "LLM request %s (attempt %d, %d bytes)", request.full_url, attempt, len(request.data))

        response = urllib.request.urlopen(request, **options)
        raw = response.read().decode("utf-8")
        elapsed = time.time() - started_at
        Logger.log("d", "LLM response HTTP %s in %.2fs (%d chars)", response.status, elapsed, len(raw))

        try:
            return json.loads(raw)
        except ValueError:
            preview = raw[:200].replace("\n", " ")
            raise LLMError("LLM вернула не JSON (проверьте Base URL, обычно он заканчивается на /v1): %s" % preview)
