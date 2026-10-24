import os

from UM.Resources import Resources
from .core import config as default_config

from .agent import Agent
from .chats import CHATS_DIR_NAME, ChatManager
from .core import EventBus
from .llm import LLMClient
from .mcp import MCPServer
from .review import ReviewGate
from .server import start_server
from .slicer import SlicerAPI
from .system_prompt import PROMPT_FILE_NAME, SystemPromptStore
from .tools import build_registry


def _default_prompt_path():
    return os.path.join(Resources.getDataStoragePath(), PROMPT_FILE_NAME)


class Context(object):
    def __init__(self, config=None, slicer_factory=SlicerAPI, llm=None, prompt_path=None, chats_dir=None):
        prompt_path = prompt_path or _default_prompt_path()
        if chats_dir is None:
            chats_dir = os.path.join(os.path.dirname(prompt_path), CHATS_DIR_NAME)

        self.config = config or default_config
        self.events = EventBus()
        self.chats = ChatManager(self.events, chats_dir)
        self.system_prompt = SystemPromptStore(prompt_path)
        self.slicer = slicer_factory(self.events, self.config)
        self.review = ReviewGate(self.events, self.slicer)
        self.registry = build_registry(self.slicer, self.events, self.review)
        self.mcp = MCPServer(self.registry)
        self.llm = llm or LLMClient(self.config)
        self.agent = Agent(self.config, self.registry, self.events, self.llm, self.system_prompt, self.chats)
        self.server = None

    def start(self):
        if self.server is None:
            addr = self.config.get("server_addr", "127.0.0.1:8765")
            self.server = start_server(self, addr=addr)
        return self

    def stop(self):
        self.chats.flush()
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
