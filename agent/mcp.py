# -*- coding: utf-8 -*-
"""Минимальный MCP-сервер (JSON-RPC 2.0): initialize, ping, tools/list, tools/call.

Официальный MCP SDK требует Python 3.10+, поэтому протокол реализован вручную.
Транспорт — HTTP POST /mcp (см. server.py); для stdio-клиентов есть mcp_bridge.py.
"""
import json

from .log import log_exception

SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "ste-slicer-ai-assistant", "version": "1.0.0"}

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _result(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


class MCPServer(object):
    def __init__(self, registry):
        self._registry = registry

    def handle_raw(self, raw):
        """raw: str с JSON. Возвращает dict/list для ответа или None (только уведомления)."""
        try:
            message = json.loads(raw)
        except ValueError:
            return _error(None, PARSE_ERROR, "Parse error")
        return self.handle(message)

    def handle(self, message):
        if isinstance(message, list):
            if not message:
                return _error(None, INVALID_REQUEST, "Empty batch")
            responses = [r for r in (self._handle_one(m) for m in message) if r is not None]
            return responses or None
        return self._handle_one(message)

    def _handle_one(self, msg):
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
            return _error(msg.get("id") if isinstance(msg, dict) else None, INVALID_REQUEST, "Invalid Request")
        msg_id = msg.get("id")
        is_notification = "id" not in msg
        method = msg["method"]
        params = msg.get("params") or {}

        try:
            if method == "initialize":
                requested = params.get("protocolVersion")
                version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
                response = _result(msg_id, {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                    "instructions": "Инструменты управляют STE Slicer: настройки, профили, материалы, "
                                    "режим 3D/5D, сцена, анализ геометрии, слайсинг и экспорт.",
                })
            elif method == "ping":
                response = _result(msg_id, {})
            elif method.startswith("notifications/"):
                return None
            elif method == "tools/list":
                response = _result(msg_id, {"tools": self._registry.mcp_tools()})
            elif method == "tools/call":
                name = params.get("name")
                if not self._registry.has(name):
                    response = _error(msg_id, INVALID_PARAMS, "Unknown tool: %s" % name)
                else:
                    outcome = self._registry.call(name, params.get("arguments") or {}, source="mcp")
                    payload = outcome.get("result") if outcome.get("ok") else {"error": outcome.get("error")}
                    content = []
                    if isinstance(payload, dict) and str(payload.get("_image", "")).startswith("data:"):
                        header, data = payload["_image"].split(",", 1)
                        content.append({"type": "image", "data": data,
                                        "mimeType": header[5:].split(";", 1)[0]})
                        payload = dict((k, v) for k, v in payload.items() if k != "_image")
                    content.insert(0, {"type": "text",
                                       "text": json.dumps(payload, ensure_ascii=False, indent=1, default=str)})
                    response = _result(msg_id, {"content": content, "isError": not outcome.get("ok")})
            else:
                response = _error(msg_id, METHOD_NOT_FOUND, "Method not found: %s" % method)
        except Exception as e:
            log_exception("MCP request failed")
            response = _error(msg_id, INTERNAL_ERROR, str(e))

        return None if is_notification else response
