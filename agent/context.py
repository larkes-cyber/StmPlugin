import os

from UM.Resources import Resources
from .core import config as default_config

from .agent import Agent
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
    def __init__(self, config=None, slicer_factory=SlicerAPI, llm=None, prompt_path=None):
        self.config = config or default_config
        self.events = EventBus()
        self.system_prompt = SystemPromptStore(prompt_path or _default_prompt_path())
        self.slicer = slicer_factory(self.events, self.config)
        self.review = ReviewGate(self.events, self.slicer)
        self.registry = build_registry(self.slicer, self.events, self.review)
        self.mcp = MCPServer(self.registry)
        self.llm = llm or LLMClient(self.config)
        self.agent = Agent(self.config, self.registry, self.events, self.llm, self.system_prompt)
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
