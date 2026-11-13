"""执行器的适配器和熔断分类（从 ops 的 tests/test_units.py 带过来），以及会话续接。"""
import json
from pathlib import Path

from superops.biz import timeutil
from superops.executor import breaker


def at(text):
    timeutil.set_fake_now(timeutil.parse_iso(text.replace(" ", "T") + ":00"))


def _spec(tmp_path, agent_cfg, tools):
    from superops.executor.adapters import RunSpec
    return RunSpec(task_id="t", run_dir=tmp_path, cwd=tmp_path, prompt="p", schema={}, schema_path=tmp_path / "s.json",
                   tools=tools, effort=None, model=None, timeout=60, agent_cfg=agent_cfg,
                   env={"HTTPS_PROXY": "http://global:1"})


def test_breaker_classify():
    cb = {"quota_patterns": ["usage limit"], "auth_patterns": ["401"], "network_patterns": ["econnrefused"]}
    assert breaker.classify("You've hit your usage limit", timed_out=False, cb=cb) == "quota"
    assert breaker.classify("HTTP 401", timed_out=False, cb=cb) == "auth"
    assert breaker.classify("connect ECONNREFUSED", timed_out=False, cb=cb) == "network"
    assert breaker.classify("", timed_out=True, cb=cb) == "timeout"
    at("2026-11-10 10:00")
    assert breaker.parse_retry_at("try again in 2 hours").startswith("2026-11-10T12:00")


def test_wrapper_and_strict_tiers(tmp_path):
    from superops.executor.adapters import AntigravityAdapter, ClaudeAdapter, wrapper_proxy
    sh = tmp_path / "agy.sh"
    sh.write_text('#!/usr/bin/env bash\nPROXY="http://10.0.0.9:3218"\nexport HTTPS_PROXY="$PROXY"\n'
                  'exec agy --dangerously-skip-permissions "$@"\n')
    assert wrapper_proxy(str(sh)) == "http://10.0.0.9:3218"
    agy = {"type": "antigravity", "bin": str(sh), "strict_bin": "/opt/agy", "strict_tiers": ["readonly"]}
    a = AntigravityAdapter()
    ro = _spec(tmp_path, agy, "readonly")
    cmd = a.build_cmd(ro)
    assert cmd[0] == "/opt/agy" and "--sandbox" in cmd and "--dangerously-skip-permissions" not in cmd
    assert a.resolve_env(ro)["HTTPS_PROXY"] == "http://10.0.0.9:3218"
    ws = _spec(tmp_path, agy, "workspace")
    cmd = a.build_cmd(ws)
    assert cmd[0] == str(sh) and "--dangerously-skip-permissions" not in cmd  # 包装脚本自己带
    assert a.resolve_env(ws)["HTTPS_PROXY"] == "http://global:1"
    cl = {"type": "claude", "bin": "~/claude.sh", "strict_bin": "/opt/claude", "strict_tiers": []}
    cmd = ClaudeAdapter().build_cmd(_spec(tmp_path, cl, "workspace"))
    assert cmd[0].endswith("claude.sh") and "Bash(security:*)" in cmd[cmd.index("--disallowedTools") + 1]


def test_codex_reconnect_notices_not_fatal(tmp_path):
    import json as _j
    from superops.executor.adapters import CodexAdapter
    out = {"status": "done", "summary": "ok", "needs_decision": False}
    fake = tmp_path / "codex.sh"
    def run(events, rc=0):
        fake.write_text(fake.read_text().split("for e in")[0] + f"for e in {events!r}: print(json.dumps(e))\nsys.exit({rc})\n")
        fake.chmod(0o755)
        return CodexAdapter().run(_spec(tmp_path, {"type": "codex", "bin": str(fake)}, "readonly"), tmp_path / "a.log")
    fake.write_text("#!/usr/bin/env python3\nimport sys, json\na = sys.argv\n"
                    f"open(a[a.index('-o') + 1], 'w').write({_j.dumps(out)!r})\n")
    r = run([{"type": "thread.started", "thread_id": "th1"}, {"type": "error", "message": "Reconnecting... 4/5"},
             {"type": "turn.completed", "usage": {}}])
    assert r.ok and r.output == out and r.session_id == "th1"
    r = run([{"type": "error", "message": "You've hit your usage limit"}])
    assert not r.ok and "usage limit" in r.error_text
    r = run([{"type": "turn.failed", "error": {"message": "boom"}}, {"type": "turn.completed"}])
    assert not r.ok


def test_codex_routing_timeout_is_network():
    from superops.biz import config
    cb = config.load_agents_config(Path(__file__).resolve().parents[2])["circuit_breaker"]
    text = '{"type":"error","message":"Reconnecting... 5/5 (workspace routing discovery timed out)"}'
    assert breaker.classify(text, timed_out=False, cb=cb) == "network"


def test_agy_readonly_settings_allow_reads_incl_symlink_targets(tmp_path):
    import json as _j
    from superops.executor.adapters import AntigravityAdapter
    outside = tmp_path / "content"
    outside.mkdir()
    (outside / "v.mp4").write_bytes(b"x")
    run = tmp_path / "run"
    (run / "media").mkdir(parents=True)
    (run / "media" / "v.mp4").symlink_to(outside / "v.mp4")
    spec = _spec(run, {"type": "antigravity", "bin": "/opt/agy.sh", "strict_bin": "/opt/agy", "strict_tiers": ["readonly"]},
                 "readonly")
    cmd = AntigravityAdapter().build_cmd(spec)
    allow = _j.loads(cmd[cmd.index("--settings") + 1])["permissions"]["allow"]
    assert f"read_file({run.resolve()}/**)" in allow and f"read_file({outside.resolve()}/**)" in allow
    assert not any(a.startswith("write_file") for a in allow)


def test_codex_failure_classified_from_events_not_stderr_noise(tmp_path):
    from superops.executor.adapters import CodexAdapter
    fake = tmp_path / "codex.sh"
    fake.write_text("#!/usr/bin/env python3\nimport sys, json\n"
                    "print(json.dumps({'type': 'turn.failed', 'error': {'message': 'model refused: bad request'}}))\n"
                    "sys.stderr.write('rmcp worker: error sending request for url (https://mcp.postman.com)\\n')\n")
    fake.chmod(0o755)
    r = CodexAdapter().run(_spec(tmp_path, {"type": "codex", "bin": str(fake)}, "readonly"), tmp_path / "a.log")
    assert not r.ok and "bad request" in r.error_text and "postman" not in r.error_text


def test_claude_resumes_ticket_session_only_when_enabled(tmp_path):
    from superops.executor.adapters import ClaudeAdapter
    on = _spec(tmp_path, {"type": "claude", "bin": "~/claude.sh", "resume": True}, "workspace")
    on.session = "sess-1"
    cmd = ClaudeAdapter().build_cmd(on)
    assert cmd[cmd.index("--resume") + 1] == "sess-1"
    off = _spec(tmp_path, {"type": "claude", "bin": "~/claude.sh"}, "workspace")
    off.session = "sess-1"
    assert "--resume" not in ClaudeAdapter().build_cmd(off)
    assert "--resume" not in ClaudeAdapter().build_cmd(_spec(tmp_path, {"type": "claude", "bin": "~/claude.sh", "resume": True}, "workspace"))


def teardown_function():
    timeutil.set_fake_now(None)


def test_工作目录里的output_json不是报告时_用最后一条消息里的报告(tmp_path):
    import json as _j
    from superops.executor.adapters import CodexAdapter, _report
    report = {"state": "ready", "summary": "监听做完了", "output": {"leads": []}}
    (tmp_path / "output.json").write_text(_j.dumps({"report_markdown": "# 报告", "leads": [], "signals": []}), encoding="utf-8")
    fake = tmp_path / "codex.sh"
    fake.write_text("#!/usr/bin/env python3\nimport sys, json\na = sys.argv\n"
                    f"open(a[a.index('-o') + 1], 'w').write({_j.dumps(report, ensure_ascii=False)!r})\n"
                    "print(json.dumps({'type': 'turn.completed'}))\n")
    fake.chmod(0o755)
    r = CodexAdapter().run(_spec(tmp_path, {"type": "codex", "bin": str(fake)}, "readonly"), tmp_path / "a.log")
    assert r.ok and r.output == report
    # 都不像报告时仍退回第一个；output.json 是报告时也照用（脚本 / 测试 Agent 的老约定）
    assert _report({"leads": []}, None) == {"leads": []}
    assert _report({"status": "done", "summary": "x"}, {"state": "ready", "summary": "y"}) == {"status": "done", "summary": "x"}


def test_长时间没有任何动作就提前结束_一直在动的不杀(tmp_path, monkeypatch):
    import sys
    import time as _t
    from superops.executor import adapters
    monkeypatch.setattr(adapters, "IDLE_CHECK_EVERY", 0.2)
    t0 = _t.monotonic()
    rc, out, err, timed_out, lost = adapters.run_process(
        [sys.executable, "-c", "import time; time.sleep(30)"], cwd=tmp_path, env={}, timeout=60, log_path=tmp_path / "quiet.log",
        idle_timeout=1.0, watch=tmp_path / "quiet")
    assert timed_out and not lost and _t.monotonic() - t0 < 10 and "没有任何动作" in err
    busy = tmp_path / "busy"
    busy.mkdir()
    code = ("import time, pathlib\n"
            f"for i in range(12):\n    pathlib.Path({str(busy)!r}, f'snap-{{i}}.yml').write_text('x'); time.sleep(0.25)\n")
    rc, out, err, timed_out, lost = adapters.run_process([sys.executable, "-c", code], cwd=tmp_path, env={}, timeout=60,
                                                         log_path=tmp_path / "busy.log", idle_timeout=1.0, watch=busy)
    assert rc == 0 and not timed_out                                 # 3 秒里一直在写页面快照：不算卡住


def test_无操作保护只对浏览器档位的Antigravity和Codex(tmp_path):
    from superops.executor.pool import AgentPool
    pool = AgentPool.__new__(AgentPool)
    pool.cfg = {"agent_browser": {"idle_timeout": "10m"}}
    assert pool._idle_timeout("browser", {"type": "antigravity"}) == 600
    assert pool._idle_timeout("browser", {"type": "codex"}) == 600
    assert pool._idle_timeout("browser", {"type": "claude"}) == 0      # Claude 最后才输出、查网页不写文件：会误杀
    assert pool._idle_timeout("workspace", {"type": "antigravity"}) == 0
    pool.cfg = {"agent_browser": {"idle_timeout": 0}}
    assert pool._idle_timeout("browser", {"type": "antigravity"}) == 0


def test_Claude浏览器档位放行网页搜索抓取和ops_browser():
    from superops.executor.adapters import CLAUDE_DENY, CLAUDE_TOOLS
    b = CLAUDE_TOOLS["browser"]
    assert {"WebSearch", "WebFetch", "Bash(ops-browser:*)"} <= set(b) and any(x.endswith("/ops-browser:*)") for x in b)
    assert "WebSearch" not in CLAUDE_TOOLS["workspace"] and "WebFetch" in CLAUDE_DENY["readonly"]
    assert not {"WebSearch", "WebFetch"} & set(CLAUDE_DENY["browser"])


def test_Codex续接_直调本体_顶层指定沙箱和目录_代理从包装脚本读(tmp_path):
    from superops.executor.adapters import CodexAdapter
    sh = tmp_path / "codex.sh"
    sh.write_text('#!/usr/bin/env bash\nPROXY="http://10.0.0.9:3218"\nexec codex -a never -s danger-full-access "$@"\n')
    cfg = {"type": "codex", "bin": str(sh), "strict_bin": "/opt/codex", "resume": True}
    a = CodexAdapter()
    first = _spec(tmp_path, cfg, "readonly")
    cmd = a.build_cmd(first, tmp_path / "x.last.txt")
    assert cmd[0] == str(sh) and cmd[1] == "exec" and "resume" not in cmd          # 第一轮照旧走包装脚本
    ro = _spec(tmp_path, cfg, "readonly")
    ro.session = "01a1cf02-734d-7821-8d86-b839a56227bc"
    cmd = a.build_cmd(ro, tmp_path / "x.last.txt")
    assert cmd[:7] == ["/opt/codex", "-a", "never", "-s", "read-only", "-C", str(tmp_path)]
    assert cmd[cmd.index("exec"):cmd.index("exec") + 3] == ["exec", "resume", ro.session] and cmd[-1] == "-"
    assert "--add-dir" not in cmd and "danger-full-access" not in cmd
    env = a.resolve_env(ro)
    assert env["HTTPS_PROXY"] == "http://10.0.0.9:3218" and env["BROWSER"] == "/bin/false"
    ws = _spec(tmp_path, cfg, "browser")
    ws.session = "s1"
    cmd = a.build_cmd(ws, tmp_path / "x.last.txt")
    assert cmd[4] == "workspace-write" and cmd.index("--add-dir") < cmd.index("exec")   # 可写目录放在顶层
    assert "sandbox_workspace_write.network_access=true" in cmd
    # workspace 档默认断网；规则 run.network: true 才放开；只读档写了也不放开
    plain = _spec(tmp_path, cfg, "workspace")
    assert "sandbox_workspace_write.network_access=true" not in a.build_cmd(plain, tmp_path / "x.last.txt")
    plain.network = True
    assert "sandbox_workspace_write.network_access=true" in a.build_cmd(plain, tmp_path / "x.last.txt")
    ro2 = _spec(tmp_path, cfg, "readonly")
    ro2.network = True
    assert "sandbox_workspace_write.network_access=true" not in a.build_cmd(ro2, tmp_path / "x.last.txt")
    off = _spec(tmp_path, {**cfg, "resume": False}, "readonly")
    off.session = "s1"
    assert "resume" not in a.build_cmd(off, tmp_path / "x.last.txt")


def test_Codex续不上就新开会话再跑一次(tmp_path, monkeypatch):
    from superops.executor.adapters import CodexAdapter, RunResult
    a = CodexAdapter()
    seen = []

    def fake(spec, log_path):
        seen.append(spec.session)
        return RunResult(ok=False, error_text="session not found") if spec.session else RunResult(ok=True, output={"state": "ready"})
    monkeypatch.setattr(a, "_run", fake)
    spec = _spec(tmp_path, {"type": "codex", "bin": "x", "strict_bin": "/opt/codex", "resume": True}, "readonly")
    spec.session = "gone"
    r = a.run(spec, tmp_path / "t.log")
    assert r.ok and seen == ["gone", None]


def test_审查类规则没给结论不能悄悄结束():
    """run.conclude_with：没发过结论事件就报 ready / dropped 的，改成 blocked；发过（本轮或之前某轮）、或交给人选的照旧。"""
    from superops.executor.worker import TicketExecutor

    class Store(dict):
        def set(self, k, v):
            self[k] = v

    ex = TicketExecutor.__new__(TicketExecutor)
    ex.rt = type("RT", (), {"store": Store()})()
    run = {"conclude_with": ["content.compliance.passed", "content.compliance.failed"]}
    silent = {"state": "dropped", "summary": "读不到脚本", "events": []}
    assert ex._require_conclusion("t1", run, silent, None)["state"] == "blocked"
    assert "读不到脚本" in ex._require_conclusion("t1", run, silent, None)["summary"]
    assert ex._require_conclusion("t1", {}, silent, None)["state"] == "dropped"
    asked = {"state": "ready", "summary": "x", "question": {"text": "能发吗"}}
    assert ex._require_conclusion("t1", run, asked, None)["state"] == "ready"
    passed = {"state": "ready", "summary": "通过", "events": [{"type": "content.compliance.passed"}]}
    assert ex._require_conclusion("t1", run, passed, None)["state"] == "ready"
    # 结论在之前某一轮发过：之后的轮次（比如重启后确认一下）可以直接结束
    assert ex._require_conclusion("t1", run, {"state": "ready", "summary": "已发过"}, None)["state"] == "ready"
    assert ex._require_conclusion("t2", run, {"state": "waiting", "summary": "等"}, None)["state"] == "waiting"
