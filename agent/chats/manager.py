import json
import os
import threading
import time
import uuid

from ..core import Logger, config
from .session import CHAT_ID_PATTERN, DEFAULT_TITLE, Chat, clean_title, json_copy

AUTO_TITLE_CHARS = 60
TRANSIENT_EVENTS = ("status", "slice_progress", "chats")
STATE_EVENTS = ("settings_changed", "profile_changed", "scene_changed")


class ChatNotFound(ValueError):
    pass


class ChatManager(object):
    def __init__(self, events, folder=None, save_delay=None):
        self._events = events
        self._lock = threading.Lock()
        self._folder = folder
        self._save_delay = float(
            save_delay
            if save_delay is not None
            else config.get("chats_save_delay", 2.0)
        )
        self._write_lock = threading.Lock()
        self._save_timer = None
        self._chats = {}
        self._active_id = None
        self._running_id = None

        self._load()
        recent = self._most_recent() or self._add_chat()
        self._active_id = recent.id

    @property
    def lock(self):
        return self._lock

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
            chats = sorted(
                self._chats.values(), key=lambda chat: chat.updated, reverse=True
            )
            return {
                "chats": [self._meta(chat) for chat in chats],
                "active_id": self._active_id,
                "running_id": self._running_id,
                "seq": getattr(self._events, "_seq", 0),
            }

    def history(self, chat_id):
        with self._lock:
            chat = self._get(chat_id)
            return {
                "chat": self._meta(chat),
                "events": list(chat.events),
                "seq": getattr(self._events, "_seq", 0),
            }

    def create(self):
        with self._lock:
            chat = self._find_reusable() or self._add_chat()
            self._active_id = chat.id
            self._notify("created", chat.id)
            return self._meta(chat)

    def select(self, chat_id):
        with self._lock:
            chat = self._get(chat_id)
            self._active_id = chat.id
            self._notify("selected", chat.id)
            return self._meta(chat)

    def rename(self, chat_id, title):
        title = clean_title(title)
        if not title:
            raise ValueError("Название чата не может быть пустым")

        with self._lock:
            chat = self._get(chat_id)
            chat.title = title
            chat.title_custom = True
            chat.updated = time.time()
            self._mark_dirty(chat)
            self._notify("renamed", chat.id)
            return self._meta(chat)

    def delete(self, chat_id):
        with self._lock:
            chat = self._get(chat_id)
            if chat.id == self._running_id:
                raise ValueError("Нельзя удалить чат, пока в нём работает агент")

            del self._chats[chat_id]
            self._remove_file(chat_id)

            if not self._chats:
                self._add_chat()

            if self._active_id == chat_id:
                fallback = self._most_recent()
                self._active_id = fallback.id if fallback else None

            self._notify("deleted", chat_id)
            return {
                "deleted": chat_id,
                "active_id": self._active_id,
                "chats": [self._meta(c) for c in self._chats.values()],
            }

    def resolve(self, chat_id=None):
        with self._lock:
            if chat_id:
                return self._get(chat_id)
            if self._active_id and self._active_id in self._chats:
                return self._chats[self._active_id]
            recent = self._most_recent() or self._add_chat()
            self._active_id = recent.id
            return recent

    def begin_run(self, chat_id=None, title_hint=""):
        """Помечает чат как запущенный и задает автозаголовок при первом сообщении."""
        with self._lock:
            chat = self._get(chat_id or self._active_id)
            if self._running_id is not None and self._running_id != chat.id:
                raise ValueError("Другой чат уже запущен")

            self._active_id = chat.id
            self._running_id = chat.id

            if not chat.title and not chat.title_custom and title_hint:
                auto = clean_title(title_hint, limit=AUTO_TITLE_CHARS)
                if auto:
                    chat.title = auto
                    chat.dirty = True

            chat.updated = time.time()
            self._notify("run_started", chat.id)
            return chat

    def checkpoint(self, chat):
        with self._lock:
            chat.saved_messages = json_copy(chat.messages)
            self._mark_dirty(chat)

    def end_run(self, chat):
        with self._lock:
            if self._running_id == chat.id:
                self._running_id = None
            chat.updated = time.time()
            chat.saved_messages = json_copy(chat.messages)
            self._mark_dirty(chat)
            self._notify("run_finished", chat.id)

    def append_event(self, event, chat_id=None):
        target_id = (
            chat_id or event.get("chat_id") or self._running_id or self._active_id
        )
        if not target_id:
            return

        event_type = event.get("type")
        if event_type in TRANSIENT_EVENTS:
            return

        with self._lock:
            chat = self._chats.get(target_id)
            if chat is None:
                return
            chat.events.append(event)
            max_events = int(config.get("chats_max_events", 3000))
            overflow = len(chat.events) - max_events
            if overflow > 0:
                del chat.events[:overflow]
            chat.updated = event.get("timestamp") or time.time()
            self._mark_dirty(chat)

    def _meta(self, chat):
        meta = chat.meta()
        meta["running"] = chat.id == self._running_id
        return meta

    def _get(self, chat_id):
        chat = self._chats.get(chat_id)
        if chat is None:
            raise ChatNotFound("Чат не найден — возможно, он был удалён")
        return chat

    def _notify(self, action, chat_id):
        self._events.emit(
            "chats", {"action": action, "target": chat_id}, chat_id=chat_id
        )

    def _add_chat(self):
        chat_id = uuid.uuid4().hex[:12]
        while chat_id in self._chats:
            chat_id = uuid.uuid4().hex[:12]
        chat = Chat(chat_id)
        self._chats[chat_id] = chat
        return chat

    def _most_recent(self):
        if not self._chats:
            return None
        return max(self._chats.values(), key=lambda chat: chat.updated)

    def _find_reusable(self):
        for chat in self._chats.values():
            if (
                chat.is_empty()
                and not chat.title_custom
                and chat.id != self._running_id
            ):
                return chat
        return None

    def _mark_dirty(self, chat):
        chat.dirty = True
        self._schedule_save()

    def _schedule_save(self):
        if self._save_delay <= 0:
            self._flush_dirty()
            return
        if self._save_timer is not None:
            return
        timer = threading.Timer(self._save_delay, self._on_timer)
        timer.daemon = True
        self._save_timer = timer
        timer.start()

    def _on_timer(self):
        with self._lock:
            self._save_timer = None
        self._flush_dirty()

    def _flush_dirty(self):
        with self._write_lock:
            with self._lock:
                folder = self._folder
                dirty_chats = [
                    c for c in self._chats.values() if c.dirty and c.should_persist()
                ]
                for c in dirty_chats:
                    c.dirty = False

            if not folder or not dirty_chats:
                return

            if not os.path.isdir(folder):
                try:
                    os.makedirs(folder, exist_ok=True)
                except OSError:
                    Logger.logException("Не удалось создать каталог чатов: %s", folder)
                    return

            for chat in dirty_chats:
                data = chat.to_dict()
                tmp_file = os.path.join(folder, chat.id + ".json.tmp")
                target_file = os.path.join(folder, chat.id + ".json")
                try:
                    with open(tmp_file, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                    os.replace(tmp_file, target_file)
                except Exception:
                    Logger.logException("Ошибка сохранения чата %s", chat.id)

    def _remove_file(self, chat_id):
        if not self._folder:
            return
        for ext in (".json", ".json.tmp"):
            path = os.path.join(self._folder, chat_id + ext)
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    def _load(self):
        if not self._folder or not os.path.isdir(self._folder):
            return
        try:
            entries = os.listdir(self._folder)
        except OSError:
            return

        for filename in entries:
            if not filename.endswith(".json") or filename.endswith(".tmp"):
                continue
            chat_id = filename[:-5]
            if not CHAT_ID_PATTERN.match(chat_id):
                continue
            filepath = os.path.join(self._folder, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                chat = Chat.from_dict(data)
                self._chats[chat.id] = chat
            except Exception:
                Logger.logException("Не удалось прочитать чат из %s", filepath)
