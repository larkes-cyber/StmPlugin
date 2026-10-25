import os

from UM.Resources import Resources

from .ai import LLMClient, PromptManager, ReviewGate, build_registry
from .chats import ChatManager
from .core import EventBus, is_main_thread
from .core.dispatcher import current_chat
from .core import config as default_config
from .runner import AgentRunner
from .server import start_server
from .slicer import SlicerAPI


def _default_prompt_path():
    return os.path.join(
        Resources.getDataStoragePath(), "ai_assistant_system_prompt.txt"
    )


class Context(object):
    def __init__(
        self,
        config=None,
        slicer_factory=SlicerAPI,
        llm=None,
        prompt_path=None,
        chats_dir=None,
    ):
        self.config = config or default_config
        prompt_path = prompt_path or _default_prompt_path()

        if chats_dir is None:
            chats_folder_name = self.config.get("chats_dir_name", "ai_assistant_chats")
            chats_dir = os.path.join(os.path.dirname(prompt_path), chats_folder_name)

        self.events = EventBus()
        self.chats = ChatManager(self.events, chats_dir)
        self.system_prompt = PromptManager(prompt_path)
        self.slicer = slicer_factory(self.events, self.config)
        self.review = ReviewGate(self.events, self.slicer)
        self.registry = build_registry(self.slicer, self.events, self.review)
        self.llm = llm or LLMClient(self.config)
        self.agent = AgentRunner(
            self.config,
            self.registry,
            self.events,
            self.llm,
            self.system_prompt,
            self.chats,
            review=self.review,
        )
        self.server = None
        self._wire_chat_attribution()

    def _wire_chat_attribution(self):
        """Каждое событие должно попадать в свой чат, даже когда чатов несколько и они работают параллельно.

        - событие из воркера чата => чат этого воркера (поток привязан через bind_wait_chat);
        - событие из главного потока Qt (сигналы бэкенда: прогресс и итог слайсинга) => чат, который
          сейчас держит слайсер;
        - иначе (внешний MCP-клиент) => "default", ChatManager покажет его в открытом чате.
        """
        gate = getattr(self.slicer, "lock", None)

        def resolve():
            chat_id = current_chat()
            if chat_id:
                return chat_id
            if gate is not None and is_main_thread():
                return getattr(gate, "owner_chat", None)
            return None

        self.events.set_chat_resolver(resolve)
        if gate is not None and hasattr(gate, "title_of"):
            gate.title_of = self._chat_title

    def _chat_title(self, chat_id):
        try:
            return self.chats.title(chat_id)
        except Exception:
            return ""

    def start(self):
        if self.server is None:
            addr = self.config.get("server_addr", "127.0.0.1:8765")
            self.server = start_server(self, addr=addr)
        return self

    def stop(self):
        if self.server is None:
            return
        self.server.shutdown()
        self.server.server_close()
        self.server = None

    @property
    def url(self):
        if self.server is None:
            return ""
        host, _ = self.config.get_server_binding()
        return "http://%s:%d" % (host, self.server.server_port)
