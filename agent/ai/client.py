import json
import ssl
import urllib.error
import urllib.request

from ..core import Logger


class LLMError(Exception):
    pass


class LLMClient(object):
    def __init__(self, config):
        self.config = config

    def chat(self, messages, tools=None):
        base_url = str(self.config.get("base_url") or "").rstrip("/")
        api_key = str(self.config.get("api_key") or "").strip()
        model = str(self.config.get("model") or "").strip()
        temperature = float(self.config.get("temperature", 0.2))
        verify_ssl = bool(self.config.get("verify_ssl", True))

        if not base_url or not model:
            raise LLMError(
                "Не заданы параметры подключения (base_url или model) в настройках"
            )

        endpoint = base_url + "/chat/completions"
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "STE-Slicer-AIAssistant",
        }
        if api_key:
            headers["Authorization"] = "Bearer " + api_key

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            endpoint, data=data, headers=headers, method="POST"
        )

        ssl_ctx = None
        if not verify_ssl:
            ssl_ctx = ssl._create_unverified_context()

        try:
            with urllib.request.urlopen(req, timeout=90.0, context=ssl_ctx) as response:
                raw_response = response.read().decode("utf-8")
                result = json.loads(raw_response)
        except urllib.error.HTTPError as error:
            try:
                err_body = error.read().decode("utf-8")
                err_json = json.loads(err_body)
                err_msg = err_json.get("error", {}).get("message", err_body)
            except Exception:
                err_msg = str(error)
            Logger.log("e", "LLM HTTP error %d: %s", error.code, err_msg)
            raise LLMError("Ошибка API модели (%d): %s" % (error.code, err_msg))
        except Exception as error:
            Logger.logException("Сбой сетевого запроса к LLM")
            raise LLMError("Не удалось связаться с LLM: %s" % error)

        choices = result.get("choices") or []
        if not choices:
            raise LLMError("Модель вернула пустой список вариантов ответа")

        message = choices[0].get("message") or {}
        usage = result.get("usage") or {}
        return message, usage
