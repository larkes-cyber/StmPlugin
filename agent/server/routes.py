import base64
import os
import re
import time
from urllib.parse import parse_qs, urlparse

from ..ai import DOC_EXT, LLMClient, extract_text
from ..chats import ChatNotFound
from ..core import Logger
from ..uploads import save_upload

IMAGE_EXT = (".png", ".jpg", ".jpeg")
MODEL_EXT = (".stl", ".obj", ".3mf")
MAX_EVENTS_WAIT = 25.0


def _safe_filename(name):
    filename = os.path.basename(name or "file")
    filename = re.sub(r"[^\w\-. ]+", "_", filename, flags=re.UNICODE).strip(" .")
    return filename or "file"


def _image_data_url(extension, raw):
    mime_type = "image/png" if extension == ".png" else "image/jpeg"
    return "data:%s;base64,%s" % (mime_type, base64.b64encode(raw).decode("ascii"))


def _collect_attachments(attachments):
    images = []
    files = []
    documents = []
    for attachment in attachments:
        filename = _safe_filename(attachment.get("name"))
        ext = os.path.splitext(filename)[1].lower()
        raw = base64.b64decode(attachment.get("data") or "")

        if ext in DOC_EXT:
            doc = extract_text(filename, raw)
            documents.append(doc)
        elif ext in IMAGE_EXT:
            images.append(_image_data_url(ext, raw))
        elif ext in MODEL_EXT:
            path = save_upload(filename, raw)
            Logger.log("d", "Файл сохранен: %s (%d байт)", path, len(raw))
            files.append({"name": filename, "path": path})
        else:
            raise ValueError("Неподдерживаемый тип файла: %s" % filename)
    return images, files, documents


def handle_get_events(ctx, handler, query):
    since_seq = int(query.get("since", [0])[0])
    wait = min(float(query.get("wait", [20.0])[0]), MAX_EVENTS_WAIT)
    client_session = query.get("session", [""])[0]
    server_session = ctx.events.session
    if client_session and client_session != server_session:
        events = []
    else:
        events = ctx.events.get_events(since_id=since_seq, timeout=wait)
    handler.send_json({
        "events": events,
        "server_time": time.time(),
        "busy": bool(ctx.agent.busy),
        "running_chat": ctx.chats.running_id,
        "running_chats": ctx.chats.running_ids,
        "session": server_session,
    })


def handle_get_config(ctx, handler, query):
    data = ctx.config.public_dict()
    data["plugin_version"] = ctx.config.version
    data["plugin_dir"] = ctx.config.plugin_dir
    data["tools"] = ctx.registry.names() if hasattr(ctx, "registry") else []
    data["system_prompt_custom"] = ctx.system_prompt.is_custom() if hasattr(ctx, "system_prompt") else False
    handler.send_json(data)


def handle_get_state(ctx, handler, query):
    handler.send_json(ctx.slicer.get_state())


def handle_get_chats(ctx, handler, query):
    handler.send_json(ctx.chats.listing())


def handle_get_chat(ctx, handler, query):
    chat_id = query.get("chat_id", query.get("id", [""]))[0]
    if not chat_id:
        handler.send_json({"error": "Параметр chat_id обязателен"}, status=400)
        return
    try:
        handler.send_json(ctx.chats.history(chat_id))
    except ChatNotFound as err:
        handler.send_json({"error": str(err)}, status=404)


def handle_get_system_prompt(ctx, handler, query):
    handler.send_json(ctx.system_prompt.state())


def handle_post_message(ctx, handler, body):
    text = (body.get("text") or "").strip()
    raw_attachments = body.get("attachments") or []
    chat_id = body.get("chat_id")

    if not text and not raw_attachments:
        handler.send_json({"error": "Пустое сообщение"}, status=400)
        return

    images, files, documents = _collect_attachments(raw_attachments)
    chat_id = ctx.agent.submit(
        text, images=images, files=files, documents=documents, chat_id=chat_id
    )
    handler.send_json({"ok": True, "chat_id": chat_id})


def handle_post_config(ctx, handler, body):
    ctx.config.update(body)
    handler.send_json({"ok": True, "config": ctx.config.public_dict()})


def handle_post_stop(ctx, handler, body):
    ctx.agent.stop(body.get("chat_id"))
    handler.send_json({"ok": True})


def handle_post_review(ctx, handler, body):
    review_id = body.get("review_id")
    if not review_id:
        handler.send_json({"error": "Некорректные параметры review"}, status=400)
        return

    if body.get("action") == "cancel" or body.get("cancelled"):
        ctx.review.cancel()
        handler.send_json({"ok": True})
        return

    try:
        ctx.review.answer(
            review_id,
            changes=body.get("changes") or body.get("params") or {},
            comment=body.get("comment") or body.get("feedback") or "",
        )
    except ValueError as error:
        handler.send_json({"error": str(error)}, status=400)
        return
    handler.send_json({"ok": True})


def handle_post_test_llm(ctx, handler, body):
    client = LLMClient(ctx.config)
    messages = [{"role": "user", "content": "Скажи одним словом: работает?"}]
    msg, usage = client.chat(messages)
    handler.send_json({"ok": True, "reply": msg.get("content", ""), "usage": usage})


def handle_post_system_prompt(ctx, handler, body):
    text = body.get("text")
    if text is None:
        handler.send_json({"error": "Параметр text обязателен"}, status=400)
        return
    ctx.system_prompt.save(text)
    handler.send_json({"ok": True, "state": ctx.system_prompt.state()})


def handle_post_system_prompt_reset(ctx, handler, body):
    ctx.system_prompt.reset()
    handler.send_json({"ok": True, "state": ctx.system_prompt.state()})


def handle_post_chats_create(ctx, handler, body):
    handler.send_json({"chat": ctx.chats.create()})


def handle_post_chats_select(ctx, handler, body):
    chat_id = body.get("chat_id")
    handler.send_json(ctx.chats.select(chat_id))


def handle_post_chats_rename(ctx, handler, body):
    chat_id = body.get("chat_id")
    title = body.get("title", "")
    handler.send_json(ctx.chats.rename(chat_id, title))


def handle_post_chats_delete(ctx, handler, body):
    chat_id = body.get("chat_id")
    handler.send_json(ctx.chats.delete(chat_id))


ROUTES_GET = {
    "/api/events": handle_get_events,
    "/api/config": handle_get_config,
    "/api/state": handle_get_state,
    "/api/chats": handle_get_chats,
    "/api/chat": handle_get_chat,
    "/api/system_prompt": handle_get_system_prompt,
}

ROUTES_POST = {
    "/api/message": handle_post_message,
    "/api/config": handle_post_config,
    "/api/stop": handle_post_stop,
    "/api/review": handle_post_review,
    "/api/test_llm": handle_post_test_llm,
    "/api/system_prompt": handle_post_system_prompt,
    "/api/system_prompt/reset": handle_post_system_prompt_reset,
    "/api/chats/create": handle_post_chats_create,
    "/api/chats/new": handle_post_chats_create,
    "/api/chats/select": handle_post_chats_select,
    "/api/chats/rename": handle_post_chats_rename,
    "/api/chats/delete": handle_post_chats_delete,
}
