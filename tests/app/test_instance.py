"""agents.yaml 的 instance：部署名推导出数据目录、钥匙串服务名、launchd 标签、积分计数器；通用层不带任何业务名。"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from superops import cli
from superops.infra import instance, paths, secrets

REPO = Path(__file__).resolve().parents[2]


def write_instance(home: Path, section) -> None:
    p = home / "agents.yaml"
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    if section is None:
        cfg.pop("instance", None)
    else:
        cfg["instance"] = section
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    (h / "agents.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("SUPEROPS_HOME", str(h))
    monkeypatch.setenv("SUPEROPS_DATA", str(tmp_path / "data"))
    return h


def test_没写instance时用不带业务名的默认值():
    inst = instance.from_config(None)
    assert (inst.name, inst.data_dir, inst.keychain_service, inst.launchd_label) == (None, "~/superops-data", "superops", "superops")
    assert (inst.credits_counter, inst.credits_label) == ("credits", "积分")


def test_按name推导_单独写的项优先():
    inst = instance.from_config({"name": "acme"})
    assert (inst.data_dir, inst.keychain_service, inst.launchd_label) == ("~/acme-superops-data", "acme-ops", "cn.acme.superops")
    inst = instance.from_config({"name": "acme", "data_dir": "~/x", "keychain_service": "acme/keys", "launchd_label": "com.acme.ops",
                                 "credits": {"counter": "acme_points", "label": "Acme 点数"}})
    assert (inst.data_dir, inst.keychain_service, inst.launchd_label) == ("~/x", "acme/keys", "com.acme.ops")
    assert (inst.credits_counter, inst.credits_label) == ("acme_points", "Acme 点数")


def test_仓库的agents_yaml推出来的就是popto原来写死的那几个值():
    inst = instance.current(REPO)
    assert (inst.data_dir, inst.keychain_service, inst.launchd_label) == ("~/popto-superops-data", "popto-ops", "cn.popto.superops")
    assert (inst.credits_counter, inst.credits_label) == ("popto_credits", "Popto 积分")
    budgets = yaml.safe_load((REPO / "agents.yaml").read_text(encoding="utf-8"))["budgets"]
    assert inst.credits_counter in budgets                                   # 预算键跟着计数器名走


@pytest.mark.parametrize("section,needle", [
    ({"name": "Popto"}, "instance.name"), ({"name": "a b"}, "instance.name"), ({"name": 3}, "instance.name"),
    ({"name": "ok", "credits": {"counter": "x-y"}}, "instance.credits.counter"),
    ({"name": "ok", "credits": "popto"}, "instance.credits"), ({"name": "ok", "data_dir": ""}, "instance.data_dir"),
    (["popto"], "映射"),
])
def test_写错了报清楚是哪一项(section, needle):
    with pytest.raises(ValueError, match=needle):
        instance.from_config(section)


def test_数据目录_环境变量优先_否则按instance(home, monkeypatch, tmp_path):
    assert paths.ops_data() == Path(str(tmp_path / "data"))                  # conftest 设了 SUPEROPS_DATA
    monkeypatch.delenv("SUPEROPS_DATA")
    monkeypatch.delenv("OPS_DATA", raising=False)
    assert paths.ops_data() == Path("~/superops-data").expanduser()
    write_instance(home, {"name": "acme", "data_dir": str(tmp_path / "acme-data")})
    assert paths.ops_data() == tmp_path / "acme-data"                        # 改了 agents.yaml 不用重启进程也认


def test_钥匙串服务名和补密钥的命令跟着instance(home, monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)

        class R:
            returncode, stdout = 0, "s3cret\n"
        return R()

    monkeypatch.setattr(secrets.subprocess, "run", fake_run)
    write_instance(home, {"name": "acme"})
    assert secrets.keychain("tikhub") == "s3cret" and calls[-1][:4] == ["security", "find-generic-password", "-s", "acme-ops/tikhub"]
    assert secrets.add_command("gitlab") == "security add-generic-password -s acme-ops/gitlab -a acme-ops -w"


def test_launchd标签跟着instance(home):
    assert cli.launchd_jobs() == {"superops": ["superops", "run"]}
    write_instance(home, {"name": "acme"})
    assert cli.launchd_jobs() == {"cn.acme.superops": ["superops", "run"]}


def test_另一个配置目录生成的plist_代码用本仓库_配置和数据用这套的(home, capsys):
    write_instance(home, {"name": "acme"})
    cli.main(["launchd", "install", "--print"])
    xml = capsys.readouterr().out
    assert "<key>Label</key><string>cn.acme.superops</string>" in xml
    assert f"<string>{REPO}</string>" in xml                                   # uv run --project 指向代码仓库
    assert f"<key>SUPEROPS_HOME</key><string>{home}</string>" in xml and f"<key>WorkingDirectory</key><string>{home}</string>" in xml


def test_重启判断认的是这套部署的launchd标签(world, monkeypatch):
    rt, home = world, world.tmp / "home"
    monkeypatch.setenv("XPC_SERVICE_NAME", "cn.popto.superops")
    assert rt.restartable() is False                                         # 没写 instance：标签是 superops
    write_instance(home, {"name": "popto"})
    assert rt.restartable() is True


def test_积分计数器和面板总览跟着instance(world):
    from superops.app import views

    rt, home = world, world.tmp / "home"
    assert views.credit_counters(rt) == {"name": "credits", "label": "积分", "used": 0, "budget": 100}   # conftest 没写 instance
    write_instance(home, {"name": "acme", "credits": {"counter": "acme_points", "label": "Acme 点数"}})
    rt.cfg.setdefault("budgets", {})["acme_points"] = 50
    rt.add_counter("acme_points", 7)
    assert views.credit_counters(rt) == {"name": "acme_points", "label": "Acme 点数", "used": 7, "budget": 50}

