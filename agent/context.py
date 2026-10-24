from .agent import Agent
from .config import Config
from .events import EventBus
from .llm import LLMClient
from .mcp import MCPServer
from .server import start_server
from .slicer import SlicerAPI
from .tools import build_registry


class Context(object):
    def __init__(self, config=None, slicer_factory=SlicerAPI, llm=None):
        self.config = config or Config()
        self.events = EventBus()
        self.slicer = slicer_factory(self.events, self.config)
        self.registry = build_registry(self.slicer, self.events)
        self.mcp = MCPServer(self.registry)
        self.llm = llm or LLMClient(self.config)
        self.agent = Agent(self.config, self.registry, self.events, self.llm)
        self.server = None

    def start(self):
        if self.server is None:
            self.server = start_server(self, self.config.get("port"))
        return self

    def stop(self):
        if self.server is None:
            return
        self.server.shutdown()
        self.server.server_close()
        self.server = None

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.server.server_port
