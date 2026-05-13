"""
Prompt 版本管理
每次 LLM 调用记录使用的 Prompt 版本，确保决策可溯源
"""
import hashlib
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)
VERSION_FILE = Path("config/llm_prompts/versions.json")


class PromptVersioner:
    def __init__(self):
        self._registry: dict = {}
        if VERSION_FILE.exists():
            self._registry = json.loads(VERSION_FILE.read_text())

    def register(self, name: str, template: str) -> str:
        version = hashlib.sha1(template.encode()).hexdigest()[:8]
        self._registry[name] = {"version": version, "hash": version}
        VERSION_FILE.write_text(json.dumps(self._registry, indent=2))
        return version

    def get_version(self, name: str) -> str:
        return self._registry.get(name, {}).get("version", "unknown")
