"""业务模块（superops.biz）测试的公共夹具：临时的仓库目录 + 数据目录，不碰真实数据、钥匙串和网络。"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
import yaml

from superops.biz import timeutil

from bizhelpers import FakeHost

REPO = Path(__file__).resolve().parents[2]
COPY = ["agents.yaml", "schedules.yaml", "decisions.md", "not-doing.md", "skills", "listening"]


@pytest.fixture(autouse=True)
def _no_keychain_in_redact(monkeypatch):
    """打码默认会读钥匙串里的真密钥（superops.biz.redact）；测试里一律不碰钥匙串。"""
    from superops.biz import redact

    monkeypatch.setattr(redact, "_known_secrets", lambda: ())


@pytest.fixture(autouse=True)
def _unfreeze_time():
    """bizhelpers.at() 冻结的时间只在当前用例里有效。"""
    yield
    timeutil.set_fake_now(None)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """临时仓库目录（SUPEROPS_HOME）和数据目录（SUPEROPS_DATA）；返回仓库目录。"""
    h = tmp_path / "ops"
    h.mkdir()
    for name in COPY:
        src = REPO / name
        if src.is_dir():
            shutil.copytree(src, h / name)
        else:
            shutil.copy2(src, h / name)
    data = tmp_path / "data"
    data.mkdir()
    (data / "local.yaml").write_text(yaml.safe_dump({
        "external_skill_dirs": [str((REPO.parent / "skills").resolve())],
        "notifier": {"adapter": "file"},
        "proxy": None,
        "agent_browser": {"cli": ["true"], "profile": str(data / "browser-profiles" / "agent-browse")},  # 测试里不真开浏览器
    }, allow_unicode=True))
    for old in ("OPS_HOME", "OPS_DATA", "OPS_TASK_FILE", "OPS_RUN_DIR", "OPS_HUB_URL", "OPS_FAKE_NOW"):
        monkeypatch.delenv(old, raising=False)
    monkeypatch.setenv("SUPEROPS_HOME", str(h))
    monkeypatch.setenv("SUPEROPS_DATA", str(data))
    return h


@pytest.fixture()
def data_dir(home) -> Path:
    return Path(os.environ["SUPEROPS_DATA"])


@pytest.fixture()
def host(monkeypatch):
    """最小的假宿主 HTTP 服务（/v1/events、notify、counter、digest、health、pause、resume）。
    OPS_HUB_URL 指向它，scriptkit.hub_client() 拿到的客户端会连到这里。"""
    h = FakeHost()
    monkeypatch.setenv("OPS_HUB_URL", h.url)
    yield h
    h.close()
