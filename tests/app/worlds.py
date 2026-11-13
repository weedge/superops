"""可以自带策略的同步 Runtime（和 conftest.py 的 world 夹具一样的搭法），给需要自己写规则的测试用。

用法：测试模块里 `from worlds import make_world  # noqa: F401`，然后 `rt = make_world(policy)`。
"""
from __future__ import annotations

import copy
import time
from types import SimpleNamespace

import pytest
import yaml

from conftest import AGENTS, POLICY


def script(code: str) -> list[str]:
    return ["{python}", "-c", code]


def done(summary: str = "做完了") -> dict:
    """一条马上报告完成的脚本执行器。"""
    return {"executor": "script", "command": script(f"import json; print(json.dumps({{'status': 'done', 'summary': {summary!r}}}))")}


def base_policy(rules: list[dict], **over) -> dict:
    policy = {"eval": {"mode": "auto", "timeout_seconds": 120}, "executor": {"max_workers": 4},
              "sources": {"schedule": {"enabled": True}, "pipeline": {"enabled": True}, "cli": {"enabled": True}},
              "policy": {"sources": {"cli": {"allowed": [{"id": "operator"}]}}, "rules": rules}}
    for key, value in over.items():
        policy[key] = {**policy.get(key, {}), **value} if isinstance(value, dict) else value
    return policy


@pytest.fixture
def make_world(tmp_path, monkeypatch):
    made = []
    # 子进程的轮询间隔从 0.5 秒调到 20 毫秒（假 Agent 和脚本几十毫秒就跑完了），其余行为不变
    from superops.executor import adapters
    monkeypatch.setattr(adapters, "time", SimpleNamespace(monotonic=time.monotonic, sleep=lambda s: time.sleep(min(s, 0.02))))

    def make(policy: dict | None = None, agents: dict | None = None, schedules: list | None = None):
        home, data = tmp_path / f"home{len(made)}", tmp_path / f"data{len(made)}"
        home.mkdir()
        (home / "prompts").mkdir()
        (home / "policy.yaml").write_text(yaml.safe_dump(policy or POLICY, allow_unicode=True), encoding="utf-8")
        (home / "agents.yaml").write_text(yaml.safe_dump(agents or copy.deepcopy(AGENTS), allow_unicode=True), encoding="utf-8")
        (home / "schedules.yaml").write_text(yaml.safe_dump(schedules or [], allow_unicode=True), encoding="utf-8")
        (home / "decisions.md").write_text("# 定的事\n", encoding="utf-8")
        (home / "not-doing.md").write_text("# 定了不做的事\n", encoding="utf-8")
        monkeypatch.setenv("SUPEROPS_HOME", str(home))
        monkeypatch.setenv("SUPEROPS_DATA", str(data))
        monkeypatch.setenv("MOCK_LOG", str(tmp_path / "mock.log"))
        from superops.app.runtime import Runtime
        rt = Runtime(home=home, sync=True)
        rt.start()
        rt.tmp = tmp_path
        made.append(rt)
        return rt

    yield make
    for rt in made:
        rt.stop()


def tickets(rt) -> list[dict]:
    """所有工单，先接的在前。"""
    return rt.store.tickets.list()[::-1]


def by_rule(rt, rule: str) -> list[dict]:
    return [t for t in tickets(rt) if rule in (t["mandate"]["basis"].get("rules") or [])]


def sent(rt, kind: str | None = None) -> list[dict]:
    return [m for m in rt.store.receipts.list(limit=500)[::-1] if m["status"] == "sent" and (kind is None or m["kind"] == kind)]


def decisions(rt, thread: str | None = None) -> list[dict]:
    """决定，先记的在前。"""
    rows = rt.store.decisions.recent(500)[::-1]
    return [d for d in rows if thread is None or d["threadKey"] == thread]
