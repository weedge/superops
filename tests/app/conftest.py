"""运行层测试：临时的仓库根和数据目录、假 Agent、同步模式的 Runtime。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

MOCK = str(Path(__file__).with_name("mock_agent.py"))

AGENTS = {
    "hub": {"host": "127.0.0.1", "port": 18787},
    "path": "",
    "agents": {
        "claude": {"type": "command", "bin": sys.executable, "args": [MOCK], "concurrency": 3},
        "codex": {"type": "command", "bin": sys.executable, "args": [MOCK], "concurrency": 2},
        "script": {"type": "script", "concurrency": 4},
    },
    "fallback_chains": {"decide": ["claude", "codex"], "write": ["claude", "codex"], "review": ["codex", "claude"],
                        "classify": ["claude", "codex"], "dev": ["claude", "codex"]},
    "circuit_breaker": {"quota_patterns": ["usage limit"], "auth_patterns": ["not logged in"], "network_patterns": ["econnrefused"]},
    "budgets": {"popto_credits": 100},
    "quiet_hours": None, "max_cards_per_day": 10, "card_default_ttl": "24h",
    "agent_browser": {"enabled": False},
}


def script(code: str) -> list[str]:
    return ["{python}", "-c", code]


POLICY = {
    "eval": {"mode": "auto", "timeout_seconds": 120},
    "executor": {"max_workers": 4},
    "sources": {"schedule": {"enabled": True}, "pipeline": {"enabled": True}, "cli": {"enabled": True}},
    "policy": {
        "sources": {"cli": {"allowed": [{"id": "operator"}], "answerers": {"batch-card": ["helper"]}},
                    "lark": {"allowed": [{"id": "ou_me", "name": "本人"}]}},
        "rules": [
            {"id": "daily-brief", "name": "日报", "instruction": "每天写日报", "when": {"origin": "cron", "types": ["cron.daily_brief"]},
             "run": {"role": "write", "agent": "claude", "tools": "readonly"}, "tell": "always"},
            {"id": "collect", "name": "采集", "instruction": "采集数据", "when": {"origin": "cron", "types": ["cron.collect"]},
             "run": {"executor": "script", "command": script(
                 "import json; print(json.dumps({'status':'done','summary':'采了 3 条','follow_up_events':[{'type':'social.metrics.anomaly','subject':'post:p1','summary':'播放异常'}]}))")}},
            {"id": "anomaly", "name": "异常复盘", "instruction": "复盘异常", "when": {"types": ["social.metrics.anomaly"]},
             "run": {"role": "write", "agent": "claude", "tools": "readonly"}},
            {"id": "batch-card", "name": "发布卡片", "instruction": "把待发布的汇成卡片请人批", "when": {"origin": "cron", "types": ["cron.publish_batch_card"]},
             "run": {"executor": "script", "command": script(
                 "import json; print(json.dumps({'status':'needs_decision','summary':'今晚 2 条待发','needs_decision':True,'decision_card':{'kind':'publish_batch','title':'今晚发布 2 条','proposal':'都发','items':[{'t':'a'},{'t':'b'}],'options':[{'key':'approve','label':'都发','emit':[{'type':'content.publish.approved','subject':'pack:a','data':{'pack':'a'}},{'type':'content.publish.approved','subject':'pack:b','data':{'pack':'b'}}]},{'key':'reject','label':'不发','require_reason':True,'record':'not_doing'}],'on_expire':'reject'}}))")},
             "tell": "needed"},
            {"id": "publish-run", "name": "执行发布", "instruction": "执行人批准的发布", "when": {"types": ["content.publish.approved"]},
             "requires_approval": True,
             "run": {"executor": "script", "command": script(
                 "import json,os; t=json.load(open(os.environ['OPS_TASK_FILE'])); e=t['events'][0]; print(json.dumps({'status':'done','summary':'已发布 '+e['data']['pack']+' 批准人 '+e['data']['approved']['by']}))")}},
            {"id": "script-writing", "name": "写脚本", "instruction": "写脚本并送审", "when": {"types": ["content.topic.approved"]},
             "run": {"role": "write", "agent": "claude", "tools": "workspace"}},
            {"id": "compliance-review", "name": "合规复核", "instruction": "复核脚本", "when": {"types": ["content.script.ready"]},
             "run": {"role": "review", "agent": "codex", "tools": "readonly"}, "max_rounds": 2},
            {"id": "note", "name": "只留底", "instruction": "只留底", "when": {"types": ["deploy.mismatch"]}, "run": {"executor": "none"},
             "tell": "always", "priority": 0},
        ],
    },
}


@pytest.fixture(autouse=True)
def _no_real_lark(monkeypatch):
    """测试里绝不调真的 lark-cli（会以本人身份往飞书发消息）：要用的测试自己替换 lark 里的函数。"""
    from superops.biz.notifier import lark

    def refuse(args, **k):
        raise lark.LarkError(f"测试里不调真的 lark-cli：{' '.join(args[:2])}")
    monkeypatch.setattr(lark, "run", refuse)
    monkeypatch.setattr(lark, "auth_status", lambda: {"ok": False, "detail": "测试"})


@pytest.fixture
def world(tmp_path, monkeypatch):
    home, data = tmp_path / "home", tmp_path / "data"
    home.mkdir()
    (home / "prompts").mkdir()
    (home / "policy.yaml").write_text(yaml.safe_dump(POLICY, allow_unicode=True), encoding="utf-8")
    (home / "agents.yaml").write_text(yaml.safe_dump(AGENTS, allow_unicode=True), encoding="utf-8")
    (home / "schedules.yaml").write_text("[]", encoding="utf-8")
    (home / "decisions.md").write_text("# 定的事\n", encoding="utf-8")
    (home / "not-doing.md").write_text("# 定了不做的事\n", encoding="utf-8")
    monkeypatch.setenv("SUPEROPS_HOME", str(home))
    monkeypatch.setenv("SUPEROPS_DATA", str(data))
    monkeypatch.setenv("MOCK_LOG", str(tmp_path / "mock.log"))
    from superops.app.runtime import Runtime
    rt = Runtime(home=home, sync=True)
    rt.start()
    rt.tmp = tmp_path
    yield rt
    rt.stop()


def prompts_seen(rt) -> list[dict]:
    log = rt.tmp / "mock.log"
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
