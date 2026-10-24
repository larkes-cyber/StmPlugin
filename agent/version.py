import json
import os

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def plugin_version():
    plugin_json_path = os.path.join(PLUGIN_DIR, "plugin.json")
    try:
        with open(plugin_json_path, encoding="utf-8") as plugin_file:
            return json.load(plugin_file).get("version", "?")
    except (OSError, ValueError):
        return "?"
