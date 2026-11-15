"""所有测试共用。"""
from __future__ import annotations

import pytest

from superops.executor import adapters

REAL_COMMAND_DIRS = adapters.command_dirs


@pytest.fixture(autouse=True)
def _no_real_agent_commands(monkeypatch):
    """找 Agent 命令时不去搜本机真实的 PATH 和安装目录：不然装了 claude / codex 的机器和没装的机器测出来不一样。
    要测“找命令”本身的用例，用 real_command_dirs 这个 fixture，或把 adapters.command_dirs 指到临时目录。"""
    monkeypatch.setattr(adapters, "command_dirs", lambda search_path="": [])


@pytest.fixture
def real_command_dirs():
    """没被上面替换掉的 command_dirs。"""
    return REAL_COMMAND_DIRS
