import os

from UM.Resources import Resources

from .ai import LLMClient, PromptManager, ReviewGate, build_registry
from .chats import ChatManager
from .core import EventBus
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
