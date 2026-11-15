"""superops doctor 的“开发与合并”几行：没接仓库只说一声；接了 GitHub 查 gh 安装 / 登录 / repo 权限范围 / 推送凭据 /
仓库写权限；接了 GitLab 查地址和令牌；每个仓库查本机路径、origin、往哪个分支合；没通过的写明怎么补。"""
from __future__ import annotations

import json
import subprocess

import pytest

from superops.biz.ci import checks

AUTH_OK = "github.com\n  ✓ Logged in to github.com account weedge (keyring)\n  - Token: gho_***\n  - Token scopes: 'gist', 'read:org', 'repo', 'workflow'\n"


def fake_gh(auth=AUTH_OK, auth_rc=0, repos=None):
    repos = repos if repos is not None else {"o/r": {"default_branch": "main", "permissions": {"push": True}}}

    def run(args, stdin=None):
        if args[:2] == ["auth", "status"]:
            return subprocess.CompletedProcess(args, auth_rc, stdout=auth, stderr="")
        path = args[args.index("-X") + 2]
        name = path.removeprefix("repos/")
        if name not in repos:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="gh: Not Found (HTTP 404)")
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(repos[name]), stderr="")
    return run


@pytest.fixture
def clone(tmp_path):
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/o/r.git"], check=True)
    return repo


def rows(cfg, *, gh=None, which=lambda cmd: f"/bin/{cmd}", secret=lambda name: None, helper=lambda host: "osxkeychain"):
    return {r["title"]: r for r in checks.check(cfg, runner=gh or fake_gh(), which=which, secret=secret, helper=helper)}


def test_没接仓库只说一声怎么接():
    r = rows({"ci": {}})
    assert list(r) == ["开发与合并"] and r["开发与合并"]["ok"] and "github:<owner>/<repo>" in r["开发与合并"]["detail"]


def test_GitHub都准备好了_每项都过_用仓库默认分支(clone):
    r = rows({"ci": {"projects": ["github:o/r"], "repos": {"o/r": str(clone)}, "base_branch": "master"}})
    assert all(x["ok"] for x in r.values()), r
    assert r["GitHub 登录（github.com）"]["detail"] == "已登录 weedge"
    assert "repo" in r["GitHub 权限范围"]["detail"] and "osxkeychain" in r["git 推送凭据（https）"]["detail"]
    assert r["仓库 o/r"]["detail"] == "能写，默认分支 main" and r["本机仓库 o/r"]["detail"] == f"{clone}，往 main 合"
    assert "GitLab 令牌" not in r                                            # 没接 GitLab 不查它的令牌


def test_gh没装_没登录_缺repo权限_没推送凭据_各自给出命令(clone):
    cfg = {"ci": {"projects": ["github:o/r"], "repos": {"o/r": str(clone)}}}
    r = rows(cfg, which=lambda cmd: None if cmd == "gh" else f"/bin/{cmd}", helper=lambda host: None)
    assert not r["GitHub 命令行 gh"]["ok"] and "brew install gh" in r["GitHub 命令行 gh"]["fix"]
    assert not r["git 推送凭据（https）"]["ok"] and r["git 推送凭据（https）"]["fix"] == "gh auth setup-git"
    assert not r["仓库 o/r"]["ok"] and "gh auth login" in r["仓库 o/r"]["fix"]
    r = rows(cfg, gh=fake_gh(auth="You are not logged into any GitHub hosts.", auth_rc=1))
    assert not r["GitHub 登录（github.com）"]["ok"] and "--git-protocol https --web" in r["GitHub 登录（github.com）"]["fix"]
    r = rows(cfg, gh=fake_gh(auth=AUTH_OK.replace("'repo', ", "")))
    assert not r["GitHub 权限范围"]["ok"] and r["GitHub 权限范围"]["fix"] == "gh auth refresh --hostname github.com --scopes repo"


def test_只能读的仓库_访问不了的仓库(clone):
    r = rows({"ci": {"projects": ["github:o/r", "github:o/x"], "repos": {"o/r": str(clone)}}},
             gh=fake_gh(repos={"o/r": {"default_branch": "main", "permissions": {"push": False}}}))
    assert not r["仓库 o/r"]["ok"] and "只能读" in r["仓库 o/r"]["detail"] and "weedge 写权限" in r["仓库 o/r"]["fix"]
    assert not r["仓库 o/x"]["ok"] and "HTTP 404" in r["仓库 o/x"]["detail"]
    assert not r["本机仓库 o/x"]["ok"] and "git clone https://github.com/o/x.git" in r["本机仓库 o/x"]["fix"]


def test_本机路径不是仓库_origin不对(clone, tmp_path):
    r = rows({"ci": {"projects": ["github:o/r"], "repos": {"o/r": str(tmp_path / "nope")}}})
    assert not r["本机仓库 o/r"]["ok"] and "不是 git 仓库" in r["本机仓库 o/r"]["detail"]
    subprocess.run(["git", "-C", str(clone), "remote", "set-url", "origin", "https://github.com/o/other.git"], check=True)
    r = rows({"ci": {"projects": ["github:o/r"], "repos": {"o/r": str(clone)}}})
    assert not r["本机仓库 o/r"]["ok"] and "o/other" in r["本机仓库 o/r"]["detail"] and "set-url" in r["本机仓库 o/r"]["fix"]


def test_接了GitLab查地址和令牌_不查gh(clone):
    subprocess.run(["git", "-C", str(clone), "remote", "set-url", "origin", "https://gl.corp/g/p.git"], check=True)
    cfg = {"ci": {"projects": ["g/p"], "gitlab_url": "https://gitlab.example.com", "repos": {"g/p": str(clone)}, "base_branch": "develop"}}
    r = rows(cfg)
    assert not r["GitLab 地址"]["ok"] and not r["GitLab 令牌"]["ok"] and "security add-generic-password" in r["GitLab 令牌"]["fix"]
    assert "GitHub 命令行 gh" not in r and r["本机仓库 g/p"]["detail"].endswith("往 develop 合")
