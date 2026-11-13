"""superops check 对 agents.yaml 里模型和思考强度的校验：角色要在降级链里、强度要是认识的档位、
主 Session 的角色要有降级链、不调用模型的 Agent 配了模型要提醒。"""
from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from superops import cli

ROOT = Path(__file__).resolve().parent.parent


def _home(tmp_path: Path, monkeypatch, mutate) -> tuple[list[str], list[str]]:
    home, data = tmp_path / "home", tmp_path / "data"
    home.mkdir(exist_ok=True)
    data.mkdir(exist_ok=True)
    for name in ("policy.yaml", "schedules.yaml"):
        shutil.copy(ROOT / name, home / name)
    for name in ("skills", "prompts"):
        if not (home / name).exists():
            (home / name).symlink_to(ROOT / name)
    cfg = yaml.safe_load((ROOT / "agents.yaml").read_text(encoding="utf-8"))
    mutate(cfg)
    (home / "agents.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("SUPEROPS_HOME", str(home))
    monkeypatch.setenv("SUPEROPS_DATA", str(data))
    return cli.check()


def test_仓库里的模型配置本身没有问题(tmp_path, monkeypatch):
    errors, warnings = _home(tmp_path, monkeypatch, lambda cfg: None)
    assert not [e for e in errors if "models" in e or "effort" in e or "主 Session" in e], errors
    assert not [w for w in warnings if "models" in w or "default_model" in w], warnings


def test_角色和强度写错_主Session没有降级链_都会报出来(tmp_path, monkeypatch):
    base_errors, _ = _home(tmp_path, monkeypatch, lambda cfg: None)

    def broken(cfg):
        cfg["agents"]["claude"]["models"]["wrtie"] = {"model": "opus", "effort": "medium"}
        cfg["agents"]["codex"]["models"]["review"]["effort"] = "huge"
        cfg["agents"]["claude"]["default_effort"] = "extreme"
        cfg["agents"]["script"]["default_model"] = "opus"
        del cfg["fallback_chains"]["route"]
    errors, warnings = _home(tmp_path, monkeypatch, broken)
    new = [e for e in errors if e not in base_errors]
    assert any("models 里的角色 wrtie" in e for e in new)
    assert any("models.review.effort 是 huge" in e for e in new)
    assert any("default_effort 是 extreme" in e for e in new)
    assert any("主 Session 的角色 route" in e for e in new)
    assert any("Agent script：脚本不调用模型" in w for w in warnings)
