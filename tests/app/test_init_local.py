"""superops init 生成的 local.yaml：默认用飞书（群「测试superops」）、lark-cli 登录着就填好群 ID 和本人 open_id、
代理取包装脚本里写的；生成的配置配上仓库的策略能过 superops check；--reset-local 先备份再重新生成。"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import pytest
import yaml

from superops import cli

ROOT = Path(__file__).resolve().parents[2]


def logged_in(group):
    return {"open_id": "ou_me", "user": "本人", "chat_id": "oc_test"}


@pytest.fixture(autouse=True)
def _no_real_gh(monkeypatch):
    """生成 GitHub 示例时不去问本机真实的 gh。"""
    monkeypatch.setattr(cli, "_probe_github", lambda host="github.com": {"host": host})


@pytest.fixture
def env(tmp_path, monkeypatch):
    home, data = tmp_path / "home", tmp_path / "data"
    home.mkdir()
    for name in ("policy.yaml", "agents.yaml", "schedules.yaml", "decisions.md", "not-doing.md"):
        shutil.copy(ROOT / name, home / name)
    for name in ("skills", "prompts"):
        (home / name).symlink_to(ROOT / name)
    agents = yaml.safe_load((home / "agents.yaml").read_text(encoding="utf-8"))       # 外部 Skill 目录按仓库解析
    agents["external_skill_dirs"] = [str((ROOT / Path(d).expanduser()).resolve()) for d in agents.get("external_skill_dirs") or []]
    (home / "agents.yaml").write_text(yaml.safe_dump(agents, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("SUPEROPS_HOME", str(home))
    monkeypatch.setenv("SUPEROPS_DATA", str(data))
    wrapper = tmp_path / "codex.sh"
    wrapper.write_text('#!/usr/bin/env bash\nPROXY="http://10.0.0.9:3218"\nexec codex "$@"\n')
    monkeypatch.setattr(cli, "WRAPPERS", (str(tmp_path / "claude.sh"), str(wrapper)))   # claude.sh 不在，codex.sh 写了代理
    monkeypatch.setattr(cli, "_probe_lark", lambda group: {})
    return data


def init(**kw):
    cli.cmd_init(argparse.Namespace(**{"chat": "lark", "group": None, "reset_local": False, **kw}))


def test_默认用飞书_群是测试superops_没登录先写群名_代理取包装脚本里的(env):
    init()
    text = (env / "local.yaml").read_text(encoding="utf-8")
    d = yaml.safe_load(text)
    assert d["home"] == {"source": "lark", "thread": "home", "address": {"chat": "测试superops"}}
    assert d["sources"]["lark"] == {"enabled": True, "chats": ["测试superops"], "observe": "mention"}
    assert d["policy"]["sources"]["lark"]["allowed"] == [] and "userOpenId" in text   # 告诉人去哪找 open_id
    assert d["proxy"] == "http://10.0.0.9:3218"
    assert "# home: {source: dingtalk" in text                                       # 切回钉钉的写法留在注释里
    assert oct((env / "local.yaml").stat().st_mode & 0o777) == "0o600"


def test_lark_cli登录着_群名换成群ID_本人open_id进审批人():
    d = yaml.safe_load(cli.render_local("lark", "测试superops", None, lark_probe=logged_in))
    assert d["home"]["address"]["chat"] == "oc_test" and d["sources"]["lark"]["chats"] == ["oc_test"]
    assert d["policy"]["sources"]["lark"]["allowed"] == [{"id": "ou_me", "name": "本人"}] and d["proxy"] is None


@pytest.mark.parametrize("probe", [lambda g: {}, logged_in])
def test_生成的飞书配置配上仓库的策略能过check(env, probe, monkeypatch):
    monkeypatch.setattr(cli, "_probe_lark", probe)
    init()
    errors, _ = cli.check()
    assert errors == []


def test_已有local_yaml不覆盖_reset_local先备份再重新生成(env):
    env.mkdir(parents=True, exist_ok=True)
    (env / "local.yaml").write_text("proxy: null\n", encoding="utf-8")
    init()
    kept = (env / "local.yaml").read_text(encoding="utf-8")
    assert kept.startswith("proxy: null\n") and yaml.safe_load(kept) == {"proxy": None}   # 不覆盖：只在末尾补了注释示例
    init(reset_local=True)
    [backup] = list(env.glob("local.yaml.bak-*"))
    assert backup.read_text(encoding="utf-8") == kept
    assert yaml.safe_load((env / "local.yaml").read_text(encoding="utf-8"))["sources"]["lark"]["enabled"] is True


def test_钉钉要给群_不配聊天软件就落本机收件箱(env):
    with pytest.raises(SystemExit, match="--group"):
        init(chat="dingtalk")
    d = yaml.safe_load(cli.render_local("dingtalk", "cid123=="))
    assert d["home"]["source"] == "dingtalk" and d["sources"]["dingtalk"]["chats"] == ["cid123=="]
    d = yaml.safe_load(cli.render_local("none"))
    assert "home" not in d and "sources" not in d


def test_附带注释掉的GitHub示例_gh登录着就用登录的账号_去掉注释就是能用的配置():
    text = cli.render_local("lark", None, None, lark_probe=lambda g: {})
    assert '#   projects: ["github:<owner>/<仓库>"]' in text and "gh auth login --hostname github.com" in text
    assert yaml.safe_load(text)["ci"]["projects"] == []                     # 只是示例：默认不接任何仓库
    text = cli.render_local("lark", None, None, lark_probe=lambda g: {}, github_probe=lambda: {"host": "github.com", "account": "weedge"})
    assert '#   projects: ["github:weedge/<仓库>"]' in text and "gh 已登录 github.com（weedge）" in text
    from superops.biz.ci import forge
    lines = text[text.index("# ---- 接 GitHub"):].splitlines()
    example = yaml.safe_load("\n".join(l[4:] for l in lines if l.startswith(("#   projects:", "#   repos:", "#   base_branch:"))))
    assert forge.projects(example) == [("github", "weedge/<仓库>")]
    assert forge.base_branch(example, "weedge/<仓库>", "github") == "main"
    assert list(example["repos"]) == ["weedge/<仓库>"]


OLD_LOCAL = """proxy: null

ci:
  gitlab_url: https://gitlab.example.com
  projects: []              # 例如 popto/frontend_popto
  auto_merge: false         # 先都请人批
  base_branch: master
  repos: {}                 # GitLab 项目 → 本机仓库路径
  verify: {}"""                                 # 旧版 init 生成的样子（末尾没换行）


def test_已有的旧版local_yaml_init时在末尾补上GitHub示例_只加注释_只加一次(env):
    env.mkdir(parents=True, exist_ok=True)
    local = env / "local.yaml"
    local.write_text(OLD_LOCAL, encoding="utf-8")
    before = yaml.safe_load(OLD_LOCAL)
    init()
    text = local.read_text(encoding="utf-8")
    assert text.startswith(OLD_LOCAL + "\n") and text.count(cli.GITHUB_MARKER) == 1
    assert '#   projects: ["github:<owner>/<仓库>"]' in text
    assert yaml.safe_load(text) == before                                  # 配置一个值都没变
    init()
    assert local.read_text(encoding="utf-8") == text                       # 再跑不重复加


def test_新生成的local_yaml已经带示例_init不再追加(env):
    init()
    text = (env / "local.yaml").read_text(encoding="utf-8")
    init()
    assert (env / "local.yaml").read_text(encoding="utf-8") == text and text.count(cli.GITHUB_MARKER) == 1
