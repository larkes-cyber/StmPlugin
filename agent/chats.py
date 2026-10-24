import json
import os
import re
import threading
import time
import uuid

from .core import Logger

CHATS_DIR_NAME = "ai_assistant_chats"
FILE_FORMAT = 1
DEFAULT_TITLE = "Новый чат"
MAX_TITLE_CHARS = 80
AUTO_TITLE_CHARS = 60
MAX_CHAT_EVENTS = 3000
SAVE_DELAY = 2.0

CHAT_ID_PATTERN = re.compile(r"^[0-9a-f]{12}$")

# Не попадают в историю чата: состояние агента и прогресс слайсинга нужны только вживую,
# а события списка чатов относятся ко всем чатам сразу.
TRANSIENT_EVENTS = ("status", "slice_progress", "chats")

# После этих событий сцена или настройки слайсера уже не те, что видел агент в других чатах.
STATE_EVENTS = ("settings_changed", "profile_changed", "scene_changed")

SHARED_SCENE_NOTE = (
    "[Служебная пометка: сцена и настройки слайсера общие для всех чатов проекта. После твоего прошлого "
    "ответа в этом чате их могли изменить — в другом чате, через MCP или после перезапуска слайсера. "
    "Не полагайся на прежние данные о сцене, настройках и слайсинге: начни с get_state.]"
)


class ChatNotFound(ValueError):
    pass


def _json_copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _history_copy(chat):
    try:
        return _json_copy(chat.messages)
    except (TypeError, ValueError):
        Logger.logException("chat %s: history is not serializable" % chat.id)
        return None


def clean_title(text, limit=MAX_TITLE_CHARS):
    title = " ".join((text or "").split())
    if len(title) > limit:
        title = title[:limit - 1].rstrip() + "…"
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
        message["content"] = "\n".join(texts) + "\n[%d изображ. было приложено ранее]" % image_count


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
        self.state_revision = None
        self.dirty = False

    def is_empty(self):
        return not self.messages and not self.events

    def should_persist(self):
        return bool(self.saved_messages or self.events or self.title_custom)

    def meta(self):
        return {
            "id": self.id,
            "title": self.title or DEFAULT_TITLE,
            "created": self.created,
            "updated": self.updated,
            "empty": self.is_empty(),
        }

    def to_json(self):
        return {
            "format": FILE_FORMAT,
            "id": self.id,
            "title": self.title,
            "title_custom": self.title_custom,
            "created": self.created,
            "updated": self.updated,
            "messages": self.saved_messages,
            "events": self.events,
        }

    @classmethod
    def from_json(cls, data):
        chat_id = str(data.get("id"))
        if not CHAT_ID_PATTERN.match(chat_id):
            raise ValueError("некорректный id чата: %s" % chat_id)

        chat = cls(chat_id, created=float(data.get("created") or time.time()))
        chat.title = clean_title(data.get("title"))
        chat.title_custom = bool(data.get("title_custom")) and bool(chat.title)
        chat.updated = float(data.get("updated") or chat.created)

        messages = [message for message in data.get("messages") or [] if isinstance(message, dict)]
        strip_images(messages)
        drop_unanswered_tool_calls(messages)
        chat.messages = messages
        chat.saved_messages = _json_copy(messages)

        events = [event for event in data.get("events") or [] if isinstance(event, dict)]
        chat.events = events[-MAX_CHAT_EVENTS:]
        return chat


class ChatManager(object):
    def __init__(self, events, folder=None, save_delay=SAVE_DELAY):
        self._events = events
        self._lock = events.lock
        self._folder = folder
        self._save_delay = save_delay
        self._write_lock = threading.Lock()
        self._save_timer = None
        self._chats = {}
        self._active_id = None
        self._running_id = None
        self._state_revision = 0

        self._load()
        recent = self._most_recent() or self._add_chat()
        self._active_id = recent.id
        events.set_router(self._route)

    @property
    def active_id(self):
        with self._lock:
            return self._active_id

    @property
    def running_id(self):
        with self._lock:
            return self._running_id

    def title(self, chat_id):
        with self._lock:
            return self._get(chat_id).meta()["title"]

    def listing(self):
        with self._lock:
            chats = sorted(self._chats.values(), key=lambda chat: chat.updated, reverse=True)
            return {
                "chats": [self._meta(chat) for chat in chats],
                "active_id": self._active_id,
                "running_id": self._running_id,
                "seq": self._events.last_seq(),
                "session": self._events.session,
            }

    def history(self, chat_id):
        with self._lock:
            chat = self._get(chat_id)
            return {
                "chat": self._meta(chat),
                "events": list(chat.events),
                "seq": self._events.last_seq(),
            }

    def create(self):
        with self._lock:
            chat = self._find_reusable() or self._add_chat()
            self._active_id = chat.id
            self._notify("created", chat.id)
            return self._meta(chat)

    def select(self, chat_id):
        with self._lock:
            self._get(chat_id)
            self._active_id = chat_id

    def rename(self, chat_id, title):
        title = clean_title(title)
        if not title:
            raise ValueError("Название чата не может быть пустым")

        with self._lock:
            chat = self._get(chat_id)
            chat.title = title
            chat.title_custom = True
            self._mark_dirty(chat)
            self._notify("renamed", chat_id)
            meta = self._meta(chat)
        Logger.log("i", "chat %s renamed: %s", chat_id, title)
        return meta

    def delete(self, chat_id):
        with self._write_lock:
            with self._lock:
                self._get(chat_id)
                if chat_id == self._running_id:
                    raise ValueError("В этом чате работает агент — остановите его, затем удалите чат")

                del self._chats[chat_id]
                if self._active_id == chat_id:
                    self._active_id = (self._most_recent() or self._add_chat()).id
                self._notify("deleted", chat_id)
            self._remove_file(chat_id)
        Logger.log("i", "chat %s deleted", chat_id)

    def begin_run(self, chat_id=None, title_hint=""):
        with self._lock:
            chat = self._get(chat_id or self._active_id)

            note = None
            if chat.messages and chat.state_revision != self._state_revision:
                note = SHARED_SCENE_NOTE
            if not chat.title:
                chat.title = clean_title(title_hint, AUTO_TITLE_CHARS)

            self._running_id = chat.id
            self._active_id = chat.id
            chat.updated = time.time()
            self._notify("run_started", chat.id)
        return chat, note

    def checkpoint(self, chat):
        snapshot = _history_copy(chat)
        if snapshot is None:
            return
        with self._lock:
            chat.saved_messages = snapshot
            self._mark_dirty(chat)

    def end_run(self, chat):
        snapshot = _history_copy(chat)
        with self._lock:
            if snapshot is not None:
                chat.saved_messages = snapshot
            chat.state_revision = self._state_revision
            chat.updated = time.time()
            if self._running_id == chat.id:
                self._running_id = None
            self._mark_dirty(chat)
            self._notify("run_finished", chat.id)
        self.flush()

    def flush(self):
        if self._folder is None:
            return

        with self._write_lock:
            with self._lock:
                if self._save_timer is not None:
                    self._save_timer.cancel()
                    self._save_timer = None

                pending = []
                for chat in self._chats.values():
                    if not chat.dirty:
                        continue
                    chat.dirty = False
                    if chat.should_persist():
                        payload = json.dumps(chat.to_json(), ensure_ascii=False, default=str)
                        pending.append((chat, payload))

            for chat, payload in pending:
                try:
                    self._write_file(chat.id, payload)
                except OSError as error:
                    Logger.log("w", "chat %s not saved: %s", chat.id, error)
                    with self._lock:
                        chat.dirty = True

    def _route(self, event):
        if "chat_id" not in event:
            event["chat_id"] = self._running_id or self._active_id

        event_type = event.get("type")
        if event_type in STATE_EVENTS:
            self._state_revision += 1
        if event_type in TRANSIENT_EVENTS:
            return

        chat = self._chats.get(event["chat_id"])
        if chat is None:
            return

        chat.events.append(event)
        overflow = len(chat.events) - MAX_CHAT_EVENTS
        if overflow > 0:
            del chat.events[:overflow]
        chat.updated = event["time"]
        self._mark_dirty(chat)

    def _meta(self, chat):
        meta = chat.meta()
        meta["running"] = chat.id == self._running_id
        return meta

    def _get(self, chat_id):
        chat = self._chats.get(chat_id)
        if chat is None:
            raise ChatNotFound("Чат не найден — возможно, его удалили в другой вкладке")
        return chat

    def _notify(self, action, chat_id):
        self._events.emit("chats", action=action, target=chat_id, chat_id=None)

    def _add_chat(self):
        chat_id = uuid.uuid4().hex[:12]
        while chat_id in self._chats:
            chat_id = uuid.uuid4().hex[:12]
        chat = Chat(chat_id)
        self._chats[chat_id] = chat
        return chat

    def _find_reusable(self):
        for chat in self._chats.values():
            if chat.is_empty() and not chat.title_custom and chat.id != self._running_id:
                return chat
        return None

    def _most_recent(self):
        if not self._chats:
            return None
        return max(self._chats.values(), key=lambda chat: chat.updated)

    def _mark_dirty(self, chat):
        chat.dirty = True
        if self._folder is None or self._save_timer is not None:
            return

        timer = threading.Timer(self._save_delay, self.flush)
        timer.daemon = True
        self._save_timer = timer
        timer.start()

    def _path(self, chat_id):
        return os.path.join(self._folder, chat_id + ".json")

    def _write_file(self, chat_id, payload):
        if not os.path.isdir(self._folder):
            os.makedirs(self._folder)

        path = self._path(chat_id)
        temp_path = path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as chat_file:
            chat_file.write(payload)
        os.replace(temp_path, path)

    def _remove_file(self, chat_id):
        if self._folder is None:
            return
        path = self._path(chat_id)
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as error:
            Logger.log("w", "chat file %s not removed: %s", path, error)

    def _load(self):
        if self._folder is None or not os.path.isdir(self._folder):
            return

        for name in sorted(os.listdir(self._folder)):
            chat_id, extension = os.path.splitext(name)
            if extension != ".json" or not CHAT_ID_PATTERN.match(chat_id):
                continue

            path = os.path.join(self._folder, name)
            try:
                with open(path, encoding="utf-8") as chat_file:
                    chat = Chat.from_json(json.load(chat_file))
                if chat.id != chat_id:
                    raise ValueError("id в файле не совпадает с именем файла")
            except (OSError, ValueError, TypeError, AttributeError) as error:
                Logger.log("w", "chat file %s skipped: %s", path, error)
                continue
            self._chats[chat.id] = chat

        Logger.log("i", "chats loaded: %d from %s", len(self._chats), self._folder)
