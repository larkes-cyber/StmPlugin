#!/usr/bin/env python3
import json
import sys
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8765/mcp"
REQUEST_TIMEOUT = 900


def _post(url, line):
    request = urllib.request.Request(
        url,
        data=line.encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return response.read().decode("utf-8")


def _request_id(line):
    try:
        return json.loads(line).get("id")
    except (ValueError, AttributeError):
        return None


def _error_body(request_id, error):
    return json.dumps({
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32000, "message": "Слайсер недоступен: %s" % error},
    })


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            body = _post(url, line)
        except Exception as error:
            request_id = _request_id(line)
            if request_id is None:
                continue
            body = _error_body(request_id, error)

        if body.strip():
            sys.stdout.write(body.strip() + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
