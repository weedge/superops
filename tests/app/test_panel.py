"""面板（superops/panel/index.html）不依赖浏览器的检查：单文件、不引外部资源、由 GET / 送出、只调用存在的接口。

在真实浏览器里看面板用演示数据服务：`uv run python tests/app/demo_server.py`。
"""
import re
import socket
import urllib.request
from pathlib import Path

from superops.app import http

PANEL = Path(http.__file__).resolve().parent.parent / "panel" / "index.html"
HTTP_SOURCE = Path(http.__file__).read_text(encoding="utf-8")


def page() -> str:
    return PANEL.read_text(encoding="utf-8")


def test_panel_is_one_file_where_the_server_looks_for_it():
    assert PANEL.is_file() and PANEL == http.PANEL
    html = page()
    assert html.lstrip().lower().startswith("<!doctype html>") and 'lang="zh-CN"' in html
    assert [p.name for p in PANEL.parent.iterdir() if p.is_file()] == ["index.html"]      # 不需要构建，也没有旁的静态文件


def test_panel_loads_nothing_from_outside():
    """内网机器离线也能用：外部地址只允许出现在注释或文字说明里，不出现在任何会发请求的位置。"""
    html = page()
    external = r"""(?:https?:)?//[^\s"'`)]+"""
    loaders = [
        rf"""\b(?:src|href|action|poster|srcset|data|formaction)\s*=\s*["'`]?\s*{external}""",   # 标签属性
        rf"""url\(\s*["']?\s*{external}""",                                                   # CSS
        rf"""@import\s+["']?\s*{external}""",
        rf"""\bfetch\(\s*["'`]\s*{external}""",                                               # 脚本
        rf"""\bimport\s*(?:\(|[\w{{}}*,\s]*from)?\s*["'`]\s*{external}""",
        rf"""\b(?:XMLHttpRequest|WebSocket|EventSource|Worker|importScripts)\b[^;\n]*{external}""",
    ]
    for pattern in loaders:
        found = re.findall(pattern, html, flags=re.I)
        assert not found, f"面板引用了外部资源：{found[:3]}"
    assert not re.search(r"<script[^>]*\bsrc\s*=", html, flags=re.I), "脚本要内联"
    assert not re.search(r"<link[^>]*rel=[\"']?stylesheet", html, flags=re.I), "样式要内联"
    assert not re.search(r"<(?:img|iframe|video|audio|object|embed)\b", html, flags=re.I)
    for href in re.findall(r"""<link[^>]*\bhref\s*=\s*["']([^"']*)""", html, flags=re.I):
        assert href.startswith("data:"), href


def routes() -> tuple[set[str], list[str]]:
    """http.py 里的 /api/ 路由：精确路径，和 re.fullmatch 用的模式。"""
    patterns = re.findall(r"""re\.fullmatch\(r"(/api/[^"]+)\"""", HTTP_SOURCE)
    exact = set(re.findall(r""""(/api/[A-Za-z0-9_/\-]+)\"""", HTTP_SOURCE)) - set(patterns)
    return exact, patterns


def referenced() -> set[str]:
    """面板里写到的每个 /api/... 路径；模板里的 ${...} 换成一个占位段，查询串去掉。"""
    found = set()
    for raw in re.findall(r"/api/(?:[\w\-./]|\$\{(?:[^{}]|\{[^{}]*\})*\})*", page()):
        found.add(re.sub(r"\$\{(?:[^{}]|\{[^{}]*\})*\}", "X", raw).rstrip("/"))
    return found


def test_every_api_path_the_panel_uses_has_a_route():
    exact, patterns = routes()
    used = referenced()
    assert len(used) >= 15, used                         # 提取本身没有失效
    missing = sorted(path for path in used if path not in exact and not any(re.fullmatch(p, path) for p in patterns))
    assert not missing, f"面板调用了 http.py 里没有的接口：{missing}"
    for needed in ("/api/overview", "/api/tickets", "/api/tickets/X", "/api/tickets/X/answer", "/api/tickets/X/reply", "/api/tickets/X/retry",
                   "/api/tickets/X/stop", "/api/threads", "/api/thread", "/api/triggers", "/api/triggers/X/reevaluate", "/api/decisions",
                   "/api/decisions/X/dismiss", "/api/sources", "/api/settings", "/api/search", "/api/business", "/api/mode", "/api/pause",
                   "/api/reload"):
        assert needed in used, f"面板没有用到 {needed}"


def test_panel_has_the_tabs_glossary_and_a_fixed_dark_scheme():
    html = page()
    for tab in ("总览", "来源", "触发", "评估", "执行", "设置", "业务"):
        assert f"'{tab}']" in html, tab
    for term in ("来源", "触发", "线程", "评估", "准入", "研判", "决定", "授权", "工单", "派工", "订阅", "扇出", "执行器", "报告", "待人决定"):
        assert f"['{term}', '" in html, term
    # 只有深色一种外观（运转台风格），浅色系统下也一样；文字和底色都走变量
    assert 'content="dark"' in html and "color-scheme: dark" in html and re.search(r"body\s*\{[^}]*background:\s*var\(--bg\)", html)
    assert "document.hidden" in html and "setInterval" in html and "5000" in html       # 每 5 秒轮询，页面不可见时暂停
    assert "alert(" not in html                                                        # 失败在页面上提示，不弹窗


def free_port() -> int:
    """自己挑一个空闲端口：serve() 收到 0 会退回配置里的固定端口，几处测试同时跑会撞上。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_root_serves_the_panel_as_html(world):
    server = http.serve(world, "127.0.0.1", free_port())
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for path in ("/", "/index.html"):
            with opener.open(f"http://127.0.0.1:{server.server_address[1]}{path}", timeout=20) as response:
                assert response.status == 200 and response.headers["Content-Type"].startswith("text/html")
                assert response.read() == PANEL.read_bytes()
    finally:
        server.shutdown()


def test_demo_seed_covers_every_state_the_panel_shows(world):
    """演示数据的造法不要悄悄坏掉：在测试夹具的 Runtime 上跑一遍 seed（demo_server 自己的策略多几条规则，这里只查两边都有的）。"""
    import importlib.util

    from superops.app import views

    spec = importlib.util.spec_from_file_location("superops_demo_server", Path(__file__).with_name("demo_server.py"))
    demo_server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo_server)

    world.home.joinpath("policy.yaml").write_text(__import__("yaml").safe_dump(demo_server.demo_policy(), allow_unicode=True), encoding="utf-8")
    world.settings.reload()
    demo_server.seed(world)
    overview = views.overview(world)
    labels = {row["label"] for row in overview["needs_person"]}
    assert {"等你回答", "受阻", "失败", "输入没送到", "等你验收"} <= labels
    assert any(p["kind"] == "eval" for p in overview["problems"]) and overview["flow"]["would_accept"] >= 3
    everything = views.tickets(world, "all", 100)
    assert {"已完成", "已合并", "被人停止", "等后续事件"} <= {row["label"] for row in everything}
    assert all(row["title"] != "假工单" for row in everything)
    assert any(t["filtered"] for t in (views.trigger_row(world, t) for t in world.store.recent_triggers(100)))
    assert any(t["pending"] for t in views.threads(world, "evaluating"))
    by_source = {s["id"]: s for s in views.sources(world)}
    assert by_source["pipeline"]["dropped_24h"] == 1 and by_source["pipeline"]["filtered_24h"] == 1 and not by_source["dingtalk"]["enabled"]
    assert any(run["is_fallback"] for run in world.store.runs(limit=200))
    assert "rule:collect" in views.settings_view(world)["paused_keys"]


def test_运转图_Agent卡片带称号职责和主跑规则(world, monkeypatch):
    from superops.app import views

    agents = world.cfg["agents"]
    name = next(iter(agents))
    monkeypatch.setitem(agents, name, {**agents[name], "title": "主笔", "duty": "写稿和研判"})
    roster = views.overview(world)["agents"]
    assert roster[name]["title"] == "主笔" and roster[name]["duty"] == "写稿和研判"
    rules = world.settings.current()["policy"]["rules"]
    for who, a in roster.items():
        mine = [r["id"] for r in rules if ((r.get("run") or {}).get("executor") == "script" and who == "script")
                or ((r.get("run") or {}).get("executor", "agent") == "agent" and (r.get("run") or {}).get("agent") == who)]
        assert [r["id"] for r in a["rules"]] == mine
        assert all(who in (world.cfg.get("fallback_chains") or {})[role][1:] for role in a["backup"])
