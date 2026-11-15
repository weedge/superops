"""Agent 专用浏览器：ops-browser 包装层（移植自 ops 的 tests/test_agent_browser.py，去掉 opsd 接入的部分）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from superops.biz import agent_browser as ab
from superops.infra import config as cfgmod


S = {"cli": ["playwright-cli"], "session": "ops-agent", "profile": Path("/data/agent-browse"), "headed": False, "enabled": True}


def test_open_pins_chrome_and_profile_whatever_agent_passes():
    cmd = ab.build_argv(["open", "https://www.doubao.com/", "--profile", "/tmp/other", "--browser=firefox", "--headed",
                         "-s=mine"], S)
    assert cmd == ["playwright-cli", "-s=ops-agent", "open", "https://www.doubao.com/", "--browser", "chrome",
                   "--profile", "/data/agent-browse"]
    assert ab.build_argv(["open"], dict(S, headed=True))[-1] == "--headed"


def test_preopened_browser_open_becomes_goto_and_close_is_noop():
    """opsd 已经开好浏览器（Codex 沙箱里重开不了）：Agent 的 open 当跳转，close 不真关。"""
    assert ab.build_argv(["open", "https://www.zhihu.com/", "--headed"], S, preopened=True)[1:] == ["-s=ops-agent", "goto", "https://www.zhihu.com/"]
    assert ab.build_argv(["open"], S, preopened=True)[2:] == ["tab-list"]
    assert ab.build_argv(["close"], S, preopened=True)[2:] == ["tab-list"]
    assert ab.build_argv(["goto", "https://a.cn"], S, preopened=True)[2:] == ["goto", "https://a.cn"]
    with pytest.raises(ValueError):
        ab.build_argv(["cookie-list"], S, preopened=True)


def test_other_commands_only_get_the_fixed_session():
    assert ab.build_argv(["click", "e12"], S) == ["playwright-cli", "-s=ops-agent", "click", "e12"]
    assert ab.build_argv(["-s=x", "screenshot", "--filename=geo/a.png"], S)[1:] == ["-s=ops-agent", "screenshot", "--filename=geo/a.png"]


@pytest.mark.parametrize("command", ["cookie-list", "state-save", "localstorage-get", "request-headers", "requests", "upload",
                                     "run-code", "delete-data", "kill-all", "route", "install-browser", "attach"])
def test_login_state_and_side_effect_commands_blocked(command):
    with pytest.raises(ValueError):
        ab.build_argv([command, "x"], S)


def test_child_env_drops_agent_proxy():
    env = ab.child_env({"HTTPS_PROXY": "http://p:1", "http_proxy": "http://p:1", "PATH": "/bin"})
    assert env == {"PATH": "/bin"}


def test_settings_defaults_and_login_mark(home, data_dir):
    s = ab.settings(cfgmod.load_agents_config(home))
    assert s["profile"] == data_dir / "browser-profiles" / "agent-browse" and s["cli"] == ["true"]
    (s["profile"] / "Default").mkdir(parents=True)  # Chrome 自己建的目录不算登录过
    assert not ab.is_initialized(s)
    assert "还没有人登录过" in ab.agent_note(cfgmod.load_agents_config(home))
    (s["profile"] / ab.LOGIN_MARK).write_text("{}")
    assert ab.is_initialized(s)


def test_main_refuses_blocked_command_without_running_anything(home, capsys):
    assert ab.main(["cookie-list"]) == 2
    assert "不提供" in capsys.readouterr().err


def test_geo_schema_separates_not_collected_from_not_mentioned(home):
    from superops.infra.validate import build_schema, validate

    schema = build_schema(home / "skills" / "ops-geo-check")
    base = {"status": "done", "summary": "采集 1/2，提到 0", "needs_decision": False, "coverage": {"planned": 2, "collected": 1}}
    ok = {"assistant": "Kimi", "question": "q", "collected": True, "mentioned": False}
    blocked = {"assistant": "豆包", "question": "q", "collected": False, "mentioned": None, "blocked_reason": "未登录"}
    assert not validate({**base, "results": [ok, blocked]}, schema)
    assert validate({**base, "results": [dict(blocked, mentioned=False)]}, schema)  # 没采到不能记成“没提到”
    assert validate({**base, "results": [{"assistant": "Kimi", "question": "q", "mentioned": False}]}, schema)
