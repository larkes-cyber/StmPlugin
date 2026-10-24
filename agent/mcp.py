import json

from agent.core import Logger

SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "ste-slicer-ai-assistant", "version": "1.0.0"}
SERVER_INSTRUCTIONS = (
    "Инструменты управляют STE Slicer: настройки, профили, материалы, "
    "режим 3D/5D, сцена, анализ геометрии, слайсинг и экспорт."
)

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _error_response(message_id, code, text):
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": code, "message": text},
    }


def _result_response(message_id, result):
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "result": result,
    }


def _image_block(data_url):
    header, data = data_url.split(",", 1)
    mime_type = header[len("data:"):].split(";", 1)[0]
    return {"type": "image", "data": data, "mimeType": mime_type}


def _is_valid_request(message):
    if not isinstance(message, dict):
        return False
    return message.get("jsonrpc") == "2.0" and "method" in message


class MCPServer(object):
    def __init__(self, registry):
        self._registry = registry

    def handle_raw(self, raw):
        try:
            message = json.loads(raw)
        except ValueError:
            return _error_response(None, PARSE_ERROR, "Parse error")
        return self.handle(message)

    def handle(self, message):
        if not isinstance(message, list):
            return self._handle_one(message)

        if not message:
            return _error_response(None, INVALID_REQUEST, "Empty batch")

        responses = []
        for item in message:
            response = self._handle_one(item)
            if response is not None:
                responses.append(response)
        return responses or None

    def _handle_one(self, message):
        if not _is_valid_request(message):
            message_id = message.get("id") if isinstance(message, dict) else None
            return _error_response(message_id, INVALID_REQUEST, "Invalid Request")

        message_id = message.get("id")
        is_notification = "id" not in message
        method = message["method"]
        params = message.get("params") or {}

        if method.startswith("notifications/"):
            return None

        try:
            response = self._dispatch(message_id, method, params)
        except Exception as error:
            Logger.logException("MCP request failed")
            response = _error_response(message_id, INTERNAL_ERROR, str(error))

        if is_notification:
            return None
        return response

    def _dispatch(self, message_id, method, params):
        if method == "initialize":
            return _result_response(message_id, self._initialize(params))

        if method == "ping":
            return _result_response(message_id, {})

        if method == "tools/list":
            return _result_response(message_id, {"tools": self._registry.mcp_tools()})

        if method == "tools/call":
            return self._call_tool(message_id, params)

        return _error_response(message_id, METHOD_NOT_FOUND, "Method not found: %s" % method)

    @staticmethod
    def _initialize(params):
        requested_version = params.get("protocolVersion")
        if requested_version in SUPPORTED_VERSIONS:
            version = requested_version
        else:
            version = SUPPORTED_VERSIONS[0]

        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": SERVER_INSTRUCTIONS,
        }

    def _call_tool(self, message_id, params):
        name = params.get("name")
        if not self._registry.has(name):
            return _error_response(message_id, INVALID_PARAMS, "Unknown tool: %s" % name)

        outcome = self._registry.call(name, params.get("arguments") or {}, source="mcp")
        if outcome.get("ok"):
            payload = outcome.get("result")
        else:
            payload = {"error": outcome.get("error")}

        image = None
        if isinstance(payload, dict) and str(payload.get("_image", "")).startswith("data:"):
            image = _image_block(payload["_image"])
            payload = {key: value for key, value in payload.items() if key != "_image"}

        content = [{
            "type": "text",
            "text": json.dumps(payload, ensure_ascii=False, indent=1, default=str),
        }]
        if image is not None:
            content.append(image)

        return _result_response(message_id, {
            "content": content,
            "isError": not outcome.get("ok"),
        })
