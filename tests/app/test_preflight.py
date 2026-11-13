"""启动检查：要提前登录的、要提前放好密钥的，没准备好就提前告诉人。"""
import pytest

from superops.app import preflight, views


@pytest.fixture
def rt(world, monkeypatch):
    """夹具里的规则只有 publish-run 用得到登录态；加上采集规则和 CI 配置，才会检查密钥。"""
    file = world.settings._file
    file["policy"]["rules"] += [
        {**file["policy"]["rules"][1], "id": "collect-comments", "name": "评论采集"},
        {**file["policy"]["rules"][1], "id": "ci-poll", "name": "发现 Agent MR"},
        {**file["policy"]["rules"][1], "id": "geo-check", "name": "GEO 检查", "enabled": False},
    ]
    world.settings._cache = None
    world.cfg["publishers"] = {"douyin": {"mode": "live"}, "wechat_channels": {"mode": "off"}, "xiaohongshu": {"mode": "dry_run"}}
    world.cfg["ci"] = {"projects": ["popto/web"]}
    world.cfg["agent_browser"] = {"enabled": True}
    keys = {"tikhub": "k"}
    monkeypatch.setattr(preflight, "keychain", lambda name: keys.get(name))
    world.keys = keys
    return world


def logins(state):
    return lambda platform: (state.get(platform, False), f"https://{platform}")


def test_lists_only_what_enabled_rules_need(rt):
    result = rt.preflight(refresh=True, check=logins({"xiaohongshu": True}))
    by_id = {i["id"]: i for i in result["items"]}
    assert by_id["key:tikhub"]["ok"] and not by_id["key:gitlab"]["ok"]
    assert by_id["login:douyin"]["ok"] is False and by_id["login:xiaohongshu"]["ok"] is True
    assert "login:wechat_channels" not in by_id            # 发布模式 off：不查
    assert "login:agent-browser" not in by_id              # 用它的规则都停用了：不查
    assert by_id["login:douyin"]["rules"] == ["执行发布"] and by_id["login:douyin"]["fix"] == "superops publish login douyin"
    assert [i["id"] for i in result["missing"]] == ["key:gitlab", "login:douyin"]
    rt.cfg["ci"] = {"projects": []}                         # 没接入仓库：GitLab 令牌不提醒
    assert "key:gitlab" not in {i["id"] for i in rt.preflight(refresh=True, check=logins({}))["items"]}


def test_missing_items_are_announced_once_a_day_and_shown_as_problems(rt):
    result = rt.preflight(refresh=True, check=logins({}))
    assert rt.announce_preflight(result) is True
    rt.pump()
    [message] = [m for m in rt.store.receipts.list() if m["status"] == "sent"]
    assert "启动检查：3 项还没准备好" in message["text"] and "superops publish login douyin" in message["text"]
    assert "security add-generic-password -s popto-ops/gitlab" in message["text"] and "k" != message["text"]
    assert rt.announce_preflight(rt.preflight(refresh=True, check=logins({}))) is False      # 同样的缺口当天不重复说
    setup = [p for p in views.problems(rt) if p["kind"] == "setup"]
    assert len(setup) == 3 and "影响：执行发布" in next(p["detail"] for p in setup if "抖音" in p["title"])
    rt.keys["gitlab"] = "t"                                                                # 补了一项：缺口变了，再说一次
    assert rt.announce_preflight(rt.preflight(refresh=True, check=logins({}))) is True


def test_all_ready_says_nothing(rt):
    rt.keys["gitlab"] = "t"
    result = rt.preflight(refresh=True, check=logins({"douyin": True, "xiaohongshu": True}))
    assert result["missing"] == [] and preflight.render(result) == ""
    assert rt.announce_preflight(result) is False
    assert [p for p in views.problems(rt) if p["kind"] == "setup"] == []


def test_login_check_failure_is_reported_not_swallowed(rt):
    def broken(platform):
        raise RuntimeError("Chrome 起不来")
    item = next(i for i in rt.preflight(refresh=True, check=broken)["items"] if i["id"] == "login:douyin")
    assert item["ok"] is False and "没查成" in item["detail"]


def test_cached_result_is_served_until_refresh(rt):
    first = rt.preflight(refresh=True, check=logins({}))
    assert rt.preflight(check=logins({"douyin": True}))["at"] == first["at"]
    assert rt.preflight(refresh=True, check=logins({"douyin": True, "xiaohongshu": True}))["missing"] != first["missing"]


def test_cli_prints_cached_result_from_running_host(rt, monkeypatch, capsys):
    from superops import cli
    from superops.app import http
    rt.preflight(refresh=True, check=logins({"douyin": True, "xiaohongshu": True}))
    server = http.serve(rt, "127.0.0.1", 0)
    try:
        from superops.biz.client import HubClient
        monkeypatch.setattr(cli, "client", lambda timeout=20: HubClient(f"http://127.0.0.1:{server.server_address[1]}", timeout=timeout))
        with pytest.raises(SystemExit) as stop:
            cli.main(["preflight"])
        out = capsys.readouterr().out
        assert stop.value.code == 1 and "✗ GitLab" in out and "✓ 抖音创作者后台登录态" in out and "1 项没准备好" in out
    finally:
        server.shutdown()
