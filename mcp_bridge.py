#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Мост stdio ↔ HTTP для MCP-клиентов, которые умеют только stdio (например, Claude Desktop).

Запуск:  python mcp_bridge.py http://127.0.0.1:8765/mcp
Слайсер с плагином должен быть запущен. Слайсер этот файл не импортирует.
"""
import json
import sys
import urllib.request

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765/mcp"


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request = urllib.request.Request(URL, data=line.encode("utf-8"),
                                         headers={"Content-Type": "application/json",
                                                  "Accept": "application/json"})
        try:
            body = urllib.request.urlopen(request, timeout=900).read().decode("utf-8")
        except Exception as e:
            try:
                msg_id = json.loads(line).get("id")
            except Exception:
                msg_id = None
            if msg_id is None:
                continue
            body = json.dumps({"jsonrpc": "2.0", "id": msg_id,
                               "error": {"code": -32000, "message": "Слайсер недоступен: %s" % e}})
        if body.strip():
            sys.stdout.write(body.strip() + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
