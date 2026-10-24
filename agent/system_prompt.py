import os

from .prompts import SYSTEM_PROMPT

PROMPT_FILE_NAME = "ai_assistant_system_prompt.txt"
MAX_PROMPT_CHARS = 50000


class SystemPromptStore(object):
    def __init__(self, path):
        self._path = path

    @property
    def path(self):
        return self._path

    def text(self):
        custom_prompt = self._read_custom()
        if custom_prompt is None:
            return SYSTEM_PROMPT
        return custom_prompt

    def is_custom(self):
        return self._read_custom() is not None

    def state(self):
        return {
            "text": self.text(),
            "default": SYSTEM_PROMPT,
            "is_custom": self.is_custom(),
            "path": self._path,
        }

    def save(self, text):
        text = (text or "").strip()
        if len(text) > MAX_PROMPT_CHARS:
            raise ValueError("Промпт слишком длинный: %d символов, максимум %d" % (len(text), MAX_PROMPT_CHARS))

        if not text or text == SYSTEM_PROMPT.strip():
            self.reset()
            return

        folder = os.path.dirname(self._path)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        with open(self._path, "w", encoding="utf-8") as prompt_file:
            prompt_file.write(text)

    def reset(self):
        if os.path.exists(self._path):
            os.remove(self._path)

    def _read_custom(self):
        if not os.path.isfile(self._path):
            return None

        with open(self._path, encoding="utf-8") as prompt_file:
            text = prompt_file.read().strip()
        return text or None
