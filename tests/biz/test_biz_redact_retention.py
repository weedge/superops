"""打码（Cookie / token / 手机号不进日志和输出）+ 数据保留期清理（08 §6 / §7）。
打码、专用浏览器、日志轮转移植自 ops 的 tests/test_retention.py；保留期清理按新接口重写
（没有 hub.db：状态库正文由 store_cleanup 回调清，目录在不在用由 is_active 回调或最近改动判断，多了 work/ 目录）。"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from datetime import timedelta
from pathlib import Path

import pytest

from superops.biz import agent_browser as ab
from superops.biz import db, retention
from superops.infra import redact
from superops.infra.timeutil import iso, now


LEAKS = [
    ('{"HOST":"channels.weixin.qq.com","cookie":"sessionid=abc123def456; wxuin=1"}', "abc123def456"),
    ("Cookie: sid_tt=abcdef1234567890; uid=1", "abcdef1234567890"),
    ('"set-cookie": ["web_session=0400697f4a2b77; path=/"]', "0400697f4a2b77"),
    ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz", "abcdefghijklmnopqrstuvwxyz"),
    ("curl -H 'x-api-key: k_live_123456789'", "k_live_123456789"),
    ('[{"name": "a1", "value": "18f1c2d3e4f5a6b7", "domain": ".xiaohongshu.com"}]', "18f1c2d3e4f5a6b7"),
    ("https://x.com/cb?access_token=abcdefgh12345678&q=1", "abcdefgh12345678"),
    ("http://user:pa55word@proxy.example:3128", "pa55word"),
    ("token glpat-abcdefghijklmnopqrstuv", "glpat-abcdefghijklmnopqrstuv"),
    ("key sk-ant-abcdefghijklmnopqrstuvwxyz", "sk-ant-abcdefghijklmnopqrstuvwxyz"),
    ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop", "eyJhbGciOiJIUzI1NiJ9"),
    ("想要资料加我 13812345678", "13812345678"),
    ("电话：+86 13912345678。", "13912345678"),
]


@pytest.mark.parametrize("text,secret", LEAKS)
def test_redact_masks_cookies_tokens_and_phones(text, secret):
    out, n = redact.redact(text, secrets=())
    assert secret not in out and n >= 1
    assert redact.redact(out, secrets=()) == (out, 0)  # 打过码的再扫一遍不变


@pytest.mark.parametrize("text", [
    "Cookie 已过期，请重新登录",
    "第一步：打开抖音创作者中心",
    "作品 7680223353274362362 播放 13，时间戳 1731312345678",
    "https://www.xiaohongshu.com/user/profile/668b9b13812345678f035a7f",
    "page_token=abcdefgh12345678",
    "https://weixin.sogou.com/link?url=abc&token=0123456789abcdef",  # 搜索结果链接里的防爬参数，打了码就打不开
    'www_authenticate_header: "Bearer resource_metadata=\\"https://mcp.postman.com/x\\""',
    '{"session_id": "0b3c9f1e-2c4d-4e5f-8a9b-0c1d2e3f4a5b", "input_tokens": 12345678901}',
])
def test_redact_leaves_normal_text_alone(text):
    assert redact.redact(text, secrets=()) == (text, 0)


def test_known_secret_values_masked_anywhere():
    out, n = redact.redact("TikHub key=XyZ-secret-value-123 已配置", secrets=("XyZ-secret-value-123",))
    assert "XyZ-secret-value-123" not in out and n == 1


def test_redact_obj_walks_nested_output():
    out, n = redact.redact_obj({"summary": "Cookie: sessionid=abcdef123456", "items": [{"note": "电话 13812345678"}, 3],
                                "ok": True})
    assert n == 2 and out["ok"] is True and out["items"][1] == 3
    assert "abcdef123456" not in json.dumps(out) and "13812345678" not in json.dumps(out)


def test_scrub_file_keeps_mode_and_mtime_and_skips_binary(tmp_path):
    p = tmp_path / "console.log"
    p.write_text('[LOG] {"cookie":"sessionid=abcdef123456"}', encoding="utf-8")
    os.chmod(p, 0o600)
    os.utime(p, (1_700_000_000, 1_700_000_000))
    assert redact.scrub_file(p, secrets=(), dry_run=True) == 1 and "abcdef123456" in p.read_text()
    assert redact.scrub_file(p, secrets=()) == 1
    assert "abcdef123456" not in p.read_text() and "<已打码>" in p.read_text()
    assert p.stat().st_mtime == 1_700_000_000 and stat.S_IMODE(p.stat().st_mode) == 0o600
    binary = tmp_path / "dump.log"
    binary.write_bytes(b"\x00\x01cookie: sessionid=abcdef123456")
    png = tmp_path / "shot.png"
    png.write_bytes(b"cookie: sessionid=abcdef123456")
    assert redact.scrub_file(binary, secrets=()) == 0 and redact.scrub_file(png, secrets=()) == 0


def test_scrub_tree_marks_and_notices_later_files(tmp_path):
    d = tmp_path / "run"
    (d / ".playwright-cli").mkdir(parents=True)
    (d / ".playwright-cli" / "page.yml").write_text("- /url: https://x/?sessionid=abcdef123456")
    (d / "notes.md").write_text("没有敏感信息")
    assert redact.needs_scrub(d)
    assert redact.scrub_tree(d, secrets=()) == {"files": 1, "hits": 1}
    assert not redact.needs_scrub(d)
    later = d / "attempt-2.log"
    later.write_text("重试")
    os.utime(later, (time.time() + 5, time.time() + 5))
    assert redact.needs_scrub(d)


# ---------------------------------------------------------------- 专用浏览器
def test_ops_browser_redacts_output_and_snapshots(home, data_dir, monkeypatch, capsys):
    run_dir = data_dir / "runs" / "t_b"
    run_dir.mkdir(parents=True)
    monkeypatch.setenv("OPS_RUN_DIR", str(run_dir))

    def fake_run(cmd, env=None, cwd=None, check=False, capture_output=False):
        d = Path(cwd) / ".playwright-cli"
        d.mkdir(exist_ok=True)
        (d / "console-1.log").write_text('[LOG] {"header":{"cookie":"sessionid=abcdef123456"}}')
        out = b"- [Console](.playwright-cli/console-1.log)\ncookie: sessionid=abcdef123456\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr=b"")

    monkeypatch.setattr(ab.subprocess, "run", fake_run)
    assert ab.main(["console"]) == 0
    out = capsys.readouterr().out
    assert "abcdef123456" not in out and "<已打码>" in out and "console-1.log" in out
    assert "abcdef123456" not in (run_dir / ".playwright-cli" / "console-1.log").read_text()


@pytest.mark.parametrize("expr", ["document.cookie", "JSON.stringify(localStorage)", "window.sessionStorage.length",
                                  "indexedDB.databases()"])
def test_ops_browser_eval_cannot_read_login_state(home, expr, capsys):
    assert ab.main(["eval", expr]) == 2
    assert "Cookie" in capsys.readouterr().err


def test_ops_browser_eval_innertext_still_allowed():
    assert ab.blocked_reason("eval", ["document.body.innerText.slice(0, 4000)"]) is None


def test_rotate_logs_copies_truncates_and_scrubs_old_copy(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    big = logs / "dingtalk-events.err.log"
    big.write_text("x" * 2048 + "\ncookie: sessionid=abcdef123456\n")
    (logs / "dingtalk-events.err.log.1").write_text("上一份")
    small = logs / "hub.err.log"
    small.write_text("小")
    assert retention.rotate_logs([logs], 1024, 2, dry_run=False) == ["logs/dingtalk-events.err.log"]
    assert big.stat().st_size == 0 and small.read_text() == "小"
    first = (logs / "dingtalk-events.err.log.1").read_text()
    assert first.startswith("x") and "abcdef123456" not in first
    assert (logs / "dingtalk-events.err.log.2").read_text() == "上一份"


def test_retention_days_capped_so_comment_copies_go_within_180_days():
    s = retention.settings({"retention": {"runs_days": 365, "text_days": 400, "log_max_mb": "x"}})
    assert s["runs_days"] == 170 and s["text_days"] == 170 and s["log_max_mb"] == 20
    assert retention.settings({}) == {"enabled": True, "runs_days": 90, "text_days": 170, "log_max_mb": 20.0, "log_keep": 2}


# ---------------------------------------------------------------- 保留期清理
def _age(path: Path, days: float) -> None:
    """把目录里所有文件（和目录自己）的修改时间拨到 days 天前。"""
    ts = time.time() - days * 86400
    for p in [*path.rglob("*"), path]:
        os.utime(p, (ts, ts))


def _seed_runs(data_dir: Path) -> dict:
    ops = db.open_ops()
    old = data_dir / "runs" / "t_20250101_000000_aaaaaa"
    (old / "scripts" / "c1").mkdir(parents=True)
    (old / "scripts" / "c1" / "douyin.md").write_text("脚本正文")
    (old / "pack" / "c1").mkdir(parents=True)
    (old / "pack" / "c1" / "meta.json").write_text("{}")
    (old / "input.md").write_text("评论原文：生日快乐")
    busy = data_dir / "runs" / "t_20250101_000001_bbbbbb"
    busy.mkdir()
    new = data_dir / "runs" / f"t_{now().strftime('%Y%m%d_%H%M%S')}_cccccc"
    new.mkdir()
    ops.execute("INSERT INTO contents(id, status, script_path) VALUES ('c1', 'scripted', ?)",
                (json.dumps([f"runs/{old.name}/scripts/c1/douyin.md"]),))
    for pid, platform in (("pk1", "douyin"), ("pk2", "xiaohongshu")):
        ops.execute("INSERT INTO publish_packs(id, content_id, platform, pack_dir, status) VALUES (?, 'c1', ?, ?, 'published')",
                    (pid, platform, str(old / "pack" / "c1")))
    return {"old": old, "busy": busy, "new": new, "ops": ops}


def test_retention_archives_referenced_files_then_deletes_old_runs(home, data_dir):
    s = _seed_runs(data_dir)
    old, ops = s["old"], s["ops"]
    asked = []

    def is_active(kind, name):  # 宿主告诉我们哪个目录还在用
        asked.append((kind, name))
        return name == s["busy"].name

    dry = retention.run(dry_run=True, cfg={}, is_active=is_active)
    assert dry["summary"].startswith("（试运行") and old.exists() and dry["details"]["runs"]["deleted"] == 1
    assert json.loads(ops.execute("SELECT script_path FROM contents").fetchone()[0])[0].startswith("runs/")

    r = retention.run(cfg={}, is_active=is_active)
    assert not old.exists() and s["busy"].exists() and s["new"].exists()
    arch = data_dir / "content" / "_archived_runs" / old.name
    assert (arch / "scripts" / "c1" / "douyin.md").read_text() == "脚本正文"
    assert (arch / "pack" / "c1" / "meta.json").exists() and not (arch / "input.md").exists()
    assert json.loads(ops.execute("SELECT script_path FROM contents").fetchone()[0]) == \
        [f"content/_archived_runs/{old.name}/scripts/c1/douyin.md"]
    assert {r_[0] for r_ in ops.execute("SELECT pack_dir FROM publish_packs")} == {str(arch / "pack" / "c1")}
    assert "删除 90 天前的运行目录 1 个" in r["summary"] and "2 份文件" in r["summary"] and "1 个还在用先留着" in r["summary"]
    assert r["details"]["runs"] == {"deleted": 1, "archived": 2, "kept_active": 1, "failed": 0}
    assert ("runs", s["busy"].name) in asked and r["details"]["store_text"] is None and "状态库" not in r["summary"]


def test_retention_purges_work_dirs_by_last_modified_and_archives_refs(home, data_dir):
    ops = db.open_ops()
    old = data_dir / "work" / "T7-写脚本"
    (old / "scripts" / "c9").mkdir(parents=True)
    (old / "scripts" / "c9" / "douyin.md").write_text("工单里写的稿")
    (old / "notes.md").write_text("草稿")
    _age(old, 100)
    waiting = data_dir / "work" / "T8-等人拍板"      # 文件也很旧，但宿主说这张工单还开着
    waiting.mkdir(parents=True)
    (waiting / "a.md").write_text("x")
    _age(waiting, 100)
    recent = data_dir / "work" / "T9-上周的"          # 目录建得早，但 80 天前还有改动：没到期
    recent.mkdir(parents=True)
    (recent / "a.md").write_text("x")
    _age(recent, 80)
    ops.execute("INSERT INTO contents(id, status, script_path) VALUES ('c9', 'scripted', ?)",
                (json.dumps([f"work/{old.name}/scripts/c9/douyin.md"], ensure_ascii=False),))
    ops.execute("INSERT INTO demands(id, title, prd_path) VALUES ('dm_1', 'x', ?)", (f"/data/homework/{old.name}/prd.md",))

    r = retention.run(cfg={}, is_active=lambda kind, name: (kind, name) == ("work", waiting.name))
    assert not old.exists() and waiting.exists() and recent.exists()
    arch = data_dir / "content" / "_archived_work" / old.name
    assert (arch / "scripts" / "c9" / "douyin.md").read_text() == "工单里写的稿" and not (arch / "notes.md").exists()
    assert json.loads(ops.execute("SELECT script_path FROM contents").fetchone()[0]) == \
        [f"content/_archived_work/{old.name}/scripts/c9/douyin.md"]
    # 只是路径里碰巧有 “…work/<名字>/” 的不算引用，不改写
    assert ops.execute("SELECT prd_path FROM demands").fetchone()[0] == f"/data/homework/{old.name}/prd.md"
    assert r["details"]["work"] == {"deleted": 1, "archived": 1, "kept_active": 1, "failed": 0}
    assert "删除 90 天前的工单目录 1 个" in r["summary"] and "content/_archived_work/" in r["summary"]


def test_scrub_marker_does_not_reset_work_dir_age(home, data_dir):
    """没打过码的旧工单目录：补打码会写 .scrubbed 标记，不能因此把目录算成“刚改过”而躲过清理。"""
    d = data_dir / "work" / "T3-旧的"
    d.mkdir(parents=True)
    (d / "agent.log").write_text("Authorization: Bearer abcdefghijklmnopqrstu")
    _age(d, 100)
    r = retention.run(cfg={})
    assert not d.exists() and r["details"]["work"]["deleted"] == 1 and r["details"]["scrub"]["hits"] == 1
    d.mkdir()
    (d / "agent.log").write_text("x")
    _age(d, 100)
    (d / redact.MARKER).touch()                      # 标记是新的，文件是旧的
    assert retention.last_touched(d) < time.time() - 99 * 86400


def test_retention_calls_store_cleanup_and_merges_counts(home, data_dir):
    ops = db.open_ops()
    old = iso(now() - timedelta(days=200))
    ops.execute("INSERT INTO signals(id, source_type, quote, kind, created_at) VALUES ('s1', 'comment', '原话', 'ask', ?)", (old,))
    ops.execute("INSERT INTO signals(id, source_type, quote, kind, created_at) VALUES ('s2', 'comment', '新的原话', 'ask', ?)", (iso(),))
    ops.execute("INSERT INTO comments(id, post_id, text, purge_after) VALUES ('cm1', 'p', '原文', ?)",
                (iso(now() - timedelta(hours=1)),))
    ops.execute("INSERT INTO comments(id, post_id, text, purge_after) VALUES ('cm2', 'p', '没到期', ?)",
                (iso(now() + timedelta(days=30)),))
    calls = []

    def store_cleanup(text_days, dry_run):
        calls.append((text_days, dry_run))
        return {"tickets": 2, "cards": 0, "triggers": 1, "自定义类别": 3}

    dry = retention.run(dry_run=True, cfg={}, store_cleanup=store_cleanup)
    assert calls == [(170, True)] and dry["details"]["ops_text"] == {"signals": 1, "comments": 1}
    assert ops.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == 2          # 试运行不改库

    r = retention.run(cfg={"retention": {"text_days": 30}}, store_cleanup=store_cleanup)
    assert calls[-1] == (30, False)
    assert r["details"]["store_text"] == {"tickets": 2, "cards": 0, "triggers": 1, "自定义类别": 3}
    assert "状态库清空 30 天前的正文：工单 2、触发 1、自定义类别 3" in r["summary"] and "卡片" not in r["summary"]
    assert [tuple(x) for x in ops.execute("SELECT id, quote, kind FROM signals ORDER BY id")] == \
        [("s1", None, "ask"), ("s2", "新的原话", "ask")]                             # 原话清空，元数据留着
    assert [x[0] for x in ops.execute("SELECT id FROM comments")] == ["cm2"]
    assert "需求信号原话 1 条" in r["summary"] and "过期评论 1 条" in r["summary"]


def test_retention_survives_store_cleanup_failure(home, data_dir):
    ops = db.open_ops()
    ops.execute("INSERT INTO comments(id, post_id, text, purge_after) VALUES ('cm1', 'p', '原文', ?)",
                (iso(now() - timedelta(hours=1)),))

    def boom(text_days, dry_run):
        raise RuntimeError("状态库锁住了")

    r = retention.run(cfg={}, store_cleanup=boom)
    assert "状态库正文清理失败" in r["summary"] and "状态库锁住了" in r["details"]["store_error"]
    assert r["details"]["store_text"] is None
    assert ops.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == 0          # 其余清理照常做完


def test_retention_rescrubs_unmarked_dirs_but_not_active_ones(home, data_dir):
    leak = "Authorization: Bearer abcdefghijklmnopqrstu"
    done_dir = data_dir / "runs" / "t_20261111_000000_dddddd"
    running = data_dir / "runs" / "t_20261111_000001_eeeeee"
    ticket = data_dir / "work" / "T12-监听"
    for d in (done_dir, running, ticket):
        d.mkdir(parents=True)
        (d / "attempt-1-codex.stderr").write_text(leak)
    r = retention.run(cfg={}, is_active=lambda kind, name: name == running.name)
    for d in (done_dir, ticket):
        assert "abcdefghijklmnopqrstu" not in (d / "attempt-1-codex.stderr").read_text()
        assert (d / redact.MARKER).exists()
    assert leak in (running / "attempt-1-codex.stderr").read_text()  # Agent 还在写，不碰
    assert "补打码：2 个文件 2 处" in r["summary"]


def test_retention_without_is_active_leaves_recently_touched_dirs_alone(home, data_dir):
    leak = "Authorization: Bearer abcdefghijklmnopqrstu"
    quiet = data_dir / "work" / "T1-昨天的"
    busy = data_dir / "work" / "T2-正在跑"
    for d in (quiet, busy):
        d.mkdir(parents=True)
        (d / "agent.log").write_text(leak)
    _age(quiet, 1)
    r = retention.run(cfg={})
    assert "abcdefghijklmnopqrstu" not in (quiet / "agent.log").read_text()
    assert leak in (busy / "agent.log").read_text() and not (busy / redact.MARKER).exists()
    assert r["details"]["scrub"] == {"dirs": 1, "files": 1, "hits": 1}
    # 回调自己出错：当成还在用，宁可不动
    _age(busy, 1)
    r = retention.run(cfg={}, is_active=lambda kind, name: 1 / 0)
    assert leak in (busy / "agent.log").read_text() and r["details"]["scrub"]["dirs"] == 0


def test_retention_disabled_and_schedule_registered(home):
    r = retention.run(cfg={"retention": {"enabled": False}}, store_cleanup=lambda *a: 1 / 0)
    assert "已停用" in r["summary"] and r["details"] == {}
    assert "cron.data_retention" in (home / "schedules.yaml").read_text(encoding="utf-8")
