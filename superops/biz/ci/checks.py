"""需求开发（建分支 → 推送 → 开 MR / PR → 审查 → 合并）要的东西准备好了没有：superops doctor 的“开发与合并”几行。

只查 ci.projects 里接了的那几种：接了 GitLab 查令牌，接了 GitHub 查 gh 的安装、登录、权限范围和 git 推送凭据；
每个仓库查托管上能不能写、本机路径和 origin 对不对、往哪个分支合。没通过的每行写明怎么补。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from ...infra.secrets import add_command, keychain
from . import forge
from .github import GitHub, run_gh


def _row(title: str, ok: bool, detail: str, fix: str | None = None) -> dict:
    return {"title": title, "ok": ok, "detail": detail, **({"fix": fix} if fix and not ok else {})}


def _git(args: list[str], cwd: Path | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=20,
                           env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    except (OSError, subprocess.TimeoutExpired) as error:
        return 1, str(error)
    return p.returncode, (p.stdout or "").strip()


def gh_auth(host: str, runner=run_gh) -> dict:
    """gh 在这个 host 上的登录：{ok, account, scopes（None = 细粒度令牌，没有 scope 一说）, detail}。不输出令牌。"""
    try:
        p = runner(["auth", "status", "--hostname", host])
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"ok": False, "detail": f"gh auth status 没查成：{error}"}
    text = f"{p.stdout or ''}\n{p.stderr or ''}"
    account = re.search(r"Logged in to \S+ account (\S+)", text)
    scopes = re.search(r"Token scopes:\s*(.*)", text)
    return {"ok": p.returncode == 0 and bool(account), "account": account.group(1) if account else None,
            "scopes": re.findall(r"'([^']+)'", scopes.group(1)) if scopes else None,
            "detail": "已登录" if p.returncode == 0 else "没登录"}


def credential_helper(host: str) -> str | None:
    rc, out = _git(["config", "--get-urlmatch", "credential.helper", f"https://{host}"])
    return out if rc == 0 and out else None


def _origin_matches(c: dict, kind: str, name: str, url: str) -> bool:
    if not url:
        return False
    try:
        k, n = forge.parse(c, url)
    except Exception:  # noqa: BLE001
        return False
    return n == name or url.rstrip("/").removesuffix(".git").endswith(name)


def check(cfg: dict, *, runner=run_gh, which=shutil.which, secret=keychain, helper=credential_helper) -> list[dict]:
    """逐项检查，返回 [{title, ok, detail, fix?}]。runner / which / secret / helper 测试时可替换。"""
    c = cfg.get("ci") or {}
    projects = forge.projects(c)
    if not projects:
        return [_row("开发与合并", True, "没接仓库（ci.projects 为空）：需求只写 PRD，不建分支、不开 MR / PR；"
                     "要接就在 local.yaml 的 ci.projects 加 github:<owner>/<repo> 或 GitLab 项目，ci.repos 配本机路径")]
    names = "、".join(f"{forge.TITLES[k]} {n}" for k, n in projects)
    merge = "闸门全过且流水线成功时自动合并" if c.get("auto_merge") else "合并都请人批（ci.auto_merge: false）"
    rows = [_row("开发与合并", True, f"{len(projects)} 个仓库：{names}；{merge}")]
    rows.append(_row("git", bool(which("git")), which("git") or "PATH 里没有 git", "xcode-select --install"))
    kinds = {k for k, _ in projects}

    if "gitlab" in kinds:
        url = str(c.get("gitlab_url") or "")
        rows.append(_row("GitLab 地址", bool(url) and "example.com" not in url, url or "没配",
                         "local.yaml 的 ci.gitlab_url 写公司 GitLab 的地址"))
        token = bool(secret("gitlab"))
        rows.append(_row("GitLab 令牌", token, "钥匙串里已有" if token else "钥匙串里没有（机器人账号，api 权限）",
                         f"{add_command('gitlab')} <令牌>"))

    gh_ok, account = False, None
    if "github" in kinds:
        host = forge.github_host(c)
        login = f"gh auth login --hostname {host} --git-protocol https --web"
        path = which("gh")
        rows.append(_row("GitHub 命令行 gh", bool(path), path or "PATH 里没有 gh", f"brew install gh，再 {login}"))
        if path:
            st = gh_auth(host, runner)
            gh_ok, account = st["ok"], st.get("account")
            rows.append(_row(f"GitHub 登录（{host}）", gh_ok, f"{st['detail']} {account or ''}".strip(), login))
            if gh_ok and st.get("scopes") is not None:
                has_repo = "repo" in st["scopes"]
                rows.append(_row("GitHub 权限范围", has_repo, "、".join(st["scopes"]) or "没有",
                                 f"gh auth refresh --hostname {host} --scopes repo"))
        used = helper(host)
        via = "gh 的凭据" if used and "gh" in used else f"{used}（之前推送成功过就行；想跟 gh 账号走：gh auth setup-git）"
        rows.append(_row("git 推送凭据（https）", bool(used), via if used else "没配凭据助手：git push 会要密码然后失败",
                         "gh auth setup-git"))

    gl_cache: dict = {}
    for kind, name in projects:
        title = f"仓库 {name}"
        default = None
        if kind == "github":
            if not gh_ok:
                rows.append(_row(title, False, "gh 没登录，查不了权限", f"gh auth login --hostname {forge.github_host(c)} --git-protocol https --web"))
            else:
                try:
                    info = GitHub(forge.github_host(c), runner=runner).repo(name)
                    default = info.get("default_branch")
                    push = bool((info.get("permissions") or {}).get("push"))
                    rows.append(_row(title, push, f"{'能写' if push else '只能读：开不了 PR、合不了'}，默认分支 {default}",
                                     f"请仓库管理员给 {account or '这个 gh 账号'} 写权限（Write）"))
                except Exception as error:  # noqa: BLE001
                    rows.append(_row(title, False, f"访问不了：{str(error)[:160]}",
                                     "检查 ci.projects 里的 owner/repo 是否写对、gh 账号能不能看到这个仓库"))
        elif secret("gitlab"):
            gl = gl_cache.setdefault("gl", forge.client(c, "gitlab"))
            try:
                info = gl._req("GET", f"/projects/{gl.pid(name)}") or {}
                default = info.get("default_branch")
                rows.append(_row(title, True, f"能访问，默认分支 {default}"))
            except Exception as error:  # noqa: BLE001
                rows.append(_row(title, False, f"访问不了：{str(error)[:160]}", "检查 ci.projects 的写法、ci.gitlab_url 和令牌权限"))

        spec = c.get("base_branch")
        base = (next((str(v) for k, v in spec.items() if forge.normalize(c, k) == name), None) if isinstance(spec, dict) else None) \
            or (default if kind == "github" else None) or (spec if isinstance(spec, str) and spec else "master")
        repo = next((Path(os.path.expanduser(str(v))) for k, v in (c.get("repos") or {}).items() if forge.normalize(c, k) == name), None)
        local = f"本机仓库 {name}"
        clone = (f"git clone https://{forge.github_host(c)}/{name}.git <路径>" if kind == "github" else "git clone <仓库地址> <路径>") \
            + f"，再在 local.yaml 的 ci.repos 写 {name}: <路径>"
        if not repo:
            rows.append(_row(local, False, "ci.repos 没配本机路径：开发工单建不了 worktree", clone))
        elif not (repo / ".git").exists():
            rows.append(_row(local, False, f"{repo} 不是 git 仓库", clone))
        else:
            _, origin = _git(["remote", "get-url", "origin"], repo)
            if not _origin_matches(c, kind, name, origin):
                rows.append(_row(local, False, f"{repo} 的 origin 是 {origin or '没有'}，不是 {name}",
                                 f"cd {repo} && git remote set-url origin <{name} 的地址>"))
            else:
                ssh = origin.startswith("git@") or origin.startswith("ssh://")
                rows.append(_row(local, True, f"{repo}，往 {base} 合" + ("（origin 走 SSH，推送用本机 SSH 密钥）" if ssh else "")))
    return rows
