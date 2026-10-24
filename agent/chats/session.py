import json
import re
import time

from ..core import config

CHAT_ID_PATTERN = re.compile(r"^[0-9a-f]{12}$")
DEFAULT_TITLE = "Новый чат"
MAX_TITLE_CHARS = 80
FILE_FORMAT = 1


def clean_title(text, limit=MAX_TITLE_CHARS):
    title = " ".join((text or "").split())
    if len(title) > limit:
        title = title[: limit - 1].rstrip() + "…"
    return title


def strip_images(messages):
    for message in messages:
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, list):
            continue

        texts = []
        image_count = 0
        for part in content:
            if part.get("type") == "text":
                texts.append(part.get("text", ""))
            elif part.get("type") == "image_url":
                image_count += 1
        message["content"] = (
            "\n".join(texts) + "\n[%d изображ. было приложено ранее]" % image_count
        )


def drop_unanswered_tool_calls(messages):
    while messages:
        last_message = messages[-1]

        if last_message.get("role") == "assistant" and last_message.get("tool_calls"):
            messages.pop()
            continue

        if last_message.get("role") != "tool":
            return

        index = len(messages) - 1
        answered_ids = set()
        while index >= 0 and messages[index].get("role") == "tool":
            answered_ids.add(messages[index].get("tool_call_id"))
            index -= 1

        if index < 0 or not messages[index].get("tool_calls"):
            return

        expected_ids = set(call.get("id") for call in messages[index]["tool_calls"])
        if expected_ids.issubset(answered_ids):
            return

        del messages[index:]


def json_copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


class Chat(object):
    def __init__(self, chat_id, created=None):
        self.id = chat_id
        self.title = ""
        self.title_custom = False
        self.created = created or time.time()
        self.updated = self.created
        self.messages = []
        self.saved_messages = []
        self.events = []
        self.slicer_state = {}
        self.dirty = False

    def is_empty(self):
        return not self.messages and not self.events and not self.slicer_state

    def should_persist(self):
        return bool(
            self.saved_messages or self.events or self.title_custom or self.slicer_state
        )

    def meta(self):
        return {
            "id": self.id,
            "title": self.title or DEFAULT_TITLE,
            "created": self.created,
            "updated": self.updated,
            "empty": self.is_empty(),
            "has_slicer_state": bool(self.slicer_state),
        }

    def update_slicer_state(self, updates):
        if not isinstance(updates, dict):
            return
        if "settings" in updates and isinstance(updates["settings"], dict):
            current_settings = self.slicer_state.setdefault("settings", {})
            current_settings.update(updates["settings"])
            other_updates = {k: v for k, v in updates.items() if k != "settings"}
            self.slicer_state.update(other_updates)
        else:
            self.slicer_state.update(updates)
        self.dirty = True

    def to_dict(self):
        return {
            "format": FILE_FORMAT,
            "id": self.id,
            "title": self.title,
            "title_custom": self.title_custom,
            "created": self.created,
            "updated": self.updated,
            "messages": self.saved_messages,
            "events": self.events,
            "slicer_state": self.slicer_state,
        }

    to_json = to_dict

    @classmethod
    def from_dict(cls, data):
        chat_id = str(data.get("id"))
        if not CHAT_ID_PATTERN.match(chat_id):
            raise ValueError("Некорректный id чата: %s" % chat_id)

        chat = cls(chat_id, created=float(data.get("created") or time.time()))
        chat.title = clean_title(data.get("title"))
        chat.title_custom = bool(data.get("title_custom")) and bool(chat.title)
        chat.updated = float(data.get("updated") or chat.created)

        messages = [msg for msg in data.get("messages") or [] if isinstance(msg, dict)]
        strip_images(messages)
        drop_unanswered_tool_calls(messages)
        chat.messages = messages
        chat.saved_messages = json_copy(messages)

        max_events = int(config.get("chats_max_events", 3000))
        events = [ev for ev in data.get("events") or [] if isinstance(ev, dict)]
        chat.events = events[-max_events:]

        slicer_state = data.get("slicer_state")
        chat.slicer_state = dict(slicer_state) if isinstance(slicer_state, dict) else {}
        return chat

    from_json = from_dict
