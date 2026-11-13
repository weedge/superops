"""确定性脚本和宿主之间的契约：环境变量 OPS_TASK_FILE / OPS_RUN_DIR / OPS_HUB_URL、stdout 最后一行 JSON、
HubClient 访问的 /v1/* 接口。宿主用一个最小的假 HTTP 服务代替（bizhelpers.FakeHost）。"""
from __future__ import annotations

import importlib
import json
import os
import pkgutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

import superops.biz
from superops.biz import db, scriptkit
from superops.biz.client import HubClient
from superops.biz.timeutil import iso
from superops.biz.notifier.listener import handle_event

from bizhelpers import FakeHost

REPO = Path(__file__).resolve().parents[2]


def test_every_biz_module_imports():
    names = [m.name for m in pkgutil.walk_packages(superops.biz.__path__, "superops.biz.") if not m.name.endswith("__main__")]
    assert {"superops.biz.bizviews", "superops.biz.context", "superops.biz.client", "superops.biz.notifier.listener",
            "superops.biz.executors.publishers.douyin", "superops.biz.ci.worktree"} <= set(names)
    assert "superops.biz.dashboard" not in names
    for name in names:
        importlib.import_module(name)


def test_old_architecture_is_gone():
    from superops.biz import config

    assert not hasattr(db, "open_hub") and not hasattr(db, "HUB_DDL") and not hasattr(db, "hub_db_path")
    assert not any(hasattr(config, n) for n in ("load_rules", "rule_chain", "check_config", "RULE_KEYS"))
    assert not any(hasattr(HubClient, n) for n in ("lease", "renew", "result"))


def test_ops_db_has_only_business_tables(home, data_dir):
    ops = db.open_ops()
    tables = {r[0] for r in ops.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"contents", "posts", "post_metrics", "comments", "signals", "demands", "leads", "funnel_daily",
            "publish_packs", "actions"} <= tables
    assert not tables & {"tasks", "events", "runs", "cards", "outbox"}
    assert db.ops_db_path() == data_dir / "ops.db" and not (data_dir / "hub.db").exists()


def test_hub_client_talks_to_host_endpoints(host):
    c = HubClient(host.url)
    assert c.health() is True
    assert c.emit({"source": "ci", "type": "deploy.verified", "subject": "mr:x!1", "dedup_key": "k1"}) == {"ok": True}
    assert c.emit({"source": "ci", "type": "deploy.verified", "subject": "mr:x!1", "dedup_key": "k1"})["duplicate"] is True
    assert [e["type"] for e in host.events] == ["deploy.verified"]
    assert c.notify("alert", "标题", "正文", priority=1, ref="r1", dedup=True)["ok"]
    assert host.notices == [{"kind": "alert", "title": "标题", "body": "正文", "priority": 1, "ref": "r1", "dedup": True}]
    assert c.get("/v1/counter", name="tikhub_requests") == {"value": 0}
    assert c.post("/v1/counter", {"name": "tikhub_requests", "value": 3})["value"] == 3
    assert c.get("/v1/counter", name="tikhub_requests")["value"] == 3
    d = c.get("/v1/digest", since="2026-11-09T10:00:00+08:00", until=None)
    assert d["since"] == "2026-11-09T10:00:00+08:00" and "tasks_by_rule" in d
    with pytest.raises(RuntimeError, match="404"):
        c.get("/v1/lease")                                   # 接单接口不存在了


def test_hub_client_sends_token_and_reports_refusal():
    h = FakeHost(token="s3cret")
    try:
        assert HubClient(h.url, "s3cret").health() is True
        assert HubClient(h.url).health() is False
        with pytest.raises(RuntimeError, match="401"):
            HubClient(h.url, "wrong").emit({"type": "x"})
    finally:
        h.close()
    assert HubClient(h.url, "s3cret", timeout=2).health() is False      # 宿主没开：探活返回 False，不抛异常


def test_scriptkit_hub_client_reads_env_then_agents_yaml(home, data_dir, host, monkeypatch):
    assert scriptkit.hub_client().base == host.url
    monkeypatch.delenv("OPS_HUB_URL")
    c = scriptkit.hub_client()
    assert c.base == "http://127.0.0.1:8797" and c.token is None           # agents.yaml 的 hub.host / port
    local = yaml.safe_load((data_dir / "local.yaml").read_text())
    local["hub"] = {"host": "10.0.0.5", "port": 9900, "token": "tk"}
    (data_dir / "local.yaml").write_text(yaml.safe_dump(local))
    c = scriptkit.hub_client()
    assert (c.base, c.token) == ("http://10.0.0.5:9900", "tk")


def test_scriptkit_load_task_and_emit_result(home, data_dir, monkeypatch, capsys):
    assert scriptkit.load_task() == {"task": {"id": "manual"}, "rule": {}, "events": []}
    task = data_dir / "task.json"
    task.write_text(json.dumps({"task": {"id": "T9"}, "rule": {"name": "r"}, "events": [{"type": "x.y", "data": {"a": 1}}]}))
    monkeypatch.setenv("OPS_TASK_FILE", str(task))
    assert scriptkit.load_task()["events"][0]["data"] == {"a": 1}
    out = scriptkit.emit_result("完成" * 400, status="needs_decision", needs_decision=True, decision_card={"title": "t"},
                                follow_up_events=[{"type": "a.b"}], message_markdown="正文", extra_field=[1])
    last = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert last == out and len(last["summary"]) == 600 and last["extra_field"] == [1]
    assert (last["status"], last["needs_decision"], last["message_markdown"]) == ("needs_decision", True, "正文")
    assert last["follow_up_events"] == [{"type": "a.b"}] and last["dogfood_friction"] == []
    monkeypatch.setenv("OPS_SECRET_TIKHUB", "from-env")
    assert scriptkit.keychain("tikhub") == "from-env"


def test_listener_bridge_reaches_host_over_http(host):
    """旧的独立监听方式：解析后的指令经 HTTP 到宿主（事件、暂停 / 恢复、批红失败的提示）。"""
    c = HubClient(host.url)

    def msg(text, mid):
        return {"type": "user_im_message_receive_group", "message_id": mid, "sender_open_dingtalk_id": "BOSS", "content": text}

    handle_event(msg("T12 1", "m1"), c, approvers=["BOSS"])
    handle_event(msg("T12 1", "m1"), c, approvers=["BOSS"])            # 事件流 + 轮询各来一次：宿主按 dedup_key 只收一条
    assert [(e["type"], e["data"]["card"], e["data"]["by"], e["dedup_key"]) for e in host.events] == \
        [("decision.made", "T12", "BOSS", "dingtalk:msg:m1")]
    handle_event(msg("暂停 douyin", "m2"), c, approvers=["BOSS"])
    assert host.paused == {"douyin": "钉钉 BOSS"}
    handle_event(msg("恢复 douyin", "m3"), c, approvers=["BOSS"])
    assert host.paused == {}
    host.event_reply = lambda evt: {"ok": False, "error": "卡片 T99 不存在"}
    handle_event(msg("T99 1", "m4"), c, approvers=["BOSS"])
    assert host.notices[-1]["title"] == "批红没成功" and host.notices[-1]["ref"] == "reply:T99"
    assert "卡片 T99 不存在" in host.notices[-1]["body"]


# ---------------------------------------------------------------- 真的以子进程拉起脚本
def _run_script(module: str, *args: str, task: dict | None = None, data_dir: Path, host_url: str | None = None) -> tuple[dict, str]:
    env = dict(os.environ, OPS_RUN_DIR=str(data_dir / "runs" / "t_script"),
               OPS_SECRET_TIKHUB="x" * 12, OPS_SECRET_GITLAB="y" * 12, OPS_SECRET_REDFOX="z" * 12)  # 子进程也不碰钥匙串
    (data_dir / "runs" / "t_script").mkdir(parents=True, exist_ok=True)
    if task is not None:
        tf = data_dir / "runs" / "t_script" / "task.json"
        tf.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
        env["OPS_TASK_FILE"] = str(tf)
    if host_url:
        env["OPS_HUB_URL"] = host_url
    p = subprocess.run([sys.executable, "-m", module, *args], cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr[-2000:]
    return json.loads(p.stdout.strip().splitlines()[-1]), p.stdout


def test_reply_executor_as_subprocess_notifies_host_and_prints_result(home, data_dir, host):
    ops = db.open_ops()
    ops.execute("INSERT INTO posts(id,platform,title,url) VALUES ('douyin_1','douyin','保险口播','https://www.douyin.com/video/1')")
    ops.execute("INSERT INTO comments(id,post_id,text,reply_status) VALUES ('douyin_1:9','douyin_1','能不能横屏','drafted')")
    ops.commit()
    task = {"task": {"id": "T21"}, "rule": {}, "events": [{
        "type": "social.reply.approved", "source": "hub", "received_at": iso(),   # 批准时间：24 小时内才执行
        "data": {"replies": [{"comment_id": "9", "text": "在做了，下个版本支持"},
                             {"comment_id": "10", "text": "加我微信细聊"}]}}]}
    out, stdout = _run_script("superops.biz.executors.reply", "run", task=task, data_dir=data_dir, host_url=host.url)
    assert out["status"] == "done" and "推送 1 条待人工回复，拦截 1 条" in out["summary"] and len(out["blocked"]) == 1
    assert out["needs_decision"] is False and out["follow_up_events"] == []
    (n,) = host.notices
    assert n["kind"] == "message" and "请回复 1 条评论" in n["title"] and "在做了" in n["body"] and n["ref"] == "replies:T21"
    assert db.open_ops().execute("SELECT reply_status FROM comments").fetchone()[0] == "approved"
    # 同一批再跑一次：按 comment_id 幂等，不重复推
    out, _ = _run_script("superops.biz.executors.reply", "run", task=task, data_dir=data_dir, host_url=host.url)
    assert "推送 0 条" in out["summary"] and len(host.notices) == 1


def test_retention_as_subprocess_prints_result_json(home, data_dir):
    d = data_dir / "work" / "T1-旧的"
    d.mkdir(parents=True)
    (d / "a.log").write_text("x")
    old = time.time() - 100 * 86400
    os.utime(d / "a.log", (old, old))
    out, _ = _run_script("superops.biz.retention", "run", "--dry-run", data_dir=data_dir)
    assert out["status"] == "done" and out["summary"].startswith("（试运行，没有改动）")
    assert out["retention"]["work"]["deleted"] == 1 and out["retention"]["store_text"] is None and d.exists()
    out, _ = _run_script("superops.biz.retention", "run", data_dir=data_dir)
    assert "删除 90 天前的工单目录 1 个" in out["summary"] and not d.exists()


def test_scripts_print_usage_and_exit_2_without_a_command(home):
    for module in ("superops.biz.retention", "superops.biz.executors.reply", "superops.biz.executors.leads",
                   "superops.biz.collectors.funnel", "superops.biz.executors.publishers"):
        p = subprocess.run([sys.executable, "-m", module], cwd=REPO, capture_output=True, text=True, timeout=60)
        assert p.returncode == 2 and "superops.biz" in p.stdout, module
