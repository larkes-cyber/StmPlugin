# -*- coding: utf-8 -*-
"""Версия плагина из plugin.json — чтобы по логу и чату было видно, какая копия загружена."""
import json
import os

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def plugin_version():
    try:
        with open(os.path.join(PLUGIN_DIR, "plugin.json"), encoding="utf-8") as f:
            return json.load(f).get("version", "?")
    except (OSError, ValueError):
        return "?"
