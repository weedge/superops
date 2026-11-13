"""开发任务的 worktree：宿主在 Agent 之前建好、之后推送并开 MR（Agent 不碰 Token、不自己 push）。

- 新需求：从 origin/<默认分支> 建 `agent/<task_id>` 分支，worktree 放在仓库同级 `<repo>-agent-<task_id>`；
- 修复 MR（事件 data 里有 project + iid）：复用 MR 的源分支，worktree 已存在就快进到远端最新；
- Agent 跑完：有新提交 → `git push -u origin <branch>`（不 force）→ 没有打开的 MR 就新建；
  没有新提交且是新建的 worktree → 删掉 worktree 和分支（只写了 PRD 的情况）。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .gitlab import GitLab


class WorktreeError(RuntimeError):
    pass


@dataclass
class Worktree:
    project: str
    repo: Path
    path: Path
    branch: str
    base: str
    base_sha: str
    fresh: bool
    iid: int | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"project": self.project, "repo": str(self.repo), "path": str(self.path), "branch": self.branch,
                "base": self.base, "base_sha": self.base_sha, "fresh": self.fresh, "iid": self.iid}


def git(cwd: Path, *args: str, check: bool = True, timeout: int = 120) -> str:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=env)
    if check and p.returncode != 0:
        raise WorktreeError(f"git {' '.join(args[:3])} 失败：{(p.stderr or p.stdout).strip()[-500:]}")
    return p.stdout.strip()


def _ci(cfg: dict) -> dict:
    return cfg.get("ci") or {}


def repo_for(cfg: dict, project: str) -> Path | None:
    repos = _ci(cfg).get("repos") or {}
    want = GitLab.normalize(project)
    for k, v in repos.items():
        if GitLab.normalize(k) == want:
            return Path(os.path.expanduser(str(v)))
    return None


def resolve_project(cfg: dict, events: list[dict]) -> tuple[str | None, int | None]:
    for e in events:
        d = e.get("data") or {}
        if d.get("project"):
            return GitLab.normalize(d["project"]), (int(d["iid"]) if d.get("iid") else None)
    c = _ci(cfg)
    if c.get("default_project"):
        return GitLab.normalize(c["default_project"]), None
    projects = c.get("projects") or []
    if len(projects) == 1:
        return GitLab.normalize(projects[0]), None
    return None, None


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-")[:60] or "wt"


def prepare(cfg: dict, task_id: str, events: list[dict], gl: GitLab | None = None) -> Worktree:
    project, iid = resolve_project(cfg, events)
    if not project:
        raise WorktreeError("不知道在哪个仓库开发：事件 data 没有 project，ci.projects 也不止一个（可设 ci.default_project）")
    repo = repo_for(cfg, project)
    if not repo or not (repo / ".git").exists():
        raise WorktreeError(f"local.yaml 的 ci.repos 没有 {project} 的本地路径（或路径不是 git 仓库）")
    c = _ci(cfg)
    gl = gl or GitLab(c.get("gitlab_url", "https://gitlab.example.com"))

    if iid:  # 修复已有 MR：用 MR 的源分支
        mr = gl.mr(project, iid)
        branch, base = mr["source_branch"], mr["target_branch"]
        path = repo.parent / f"{repo.name}-agent-{_slug(branch.split('/', 1)[-1])}"
        git(repo, "fetch", "origin", branch, base)
        if path.exists():
            if git(path, "status", "--porcelain"):
                raise WorktreeError(f"{path} 有未提交的改动，先人工处理")
            git(path, "checkout", branch)
            git(path, "merge", "--ff-only", f"origin/{branch}")
        else:
            git(repo, "worktree", "add", str(path), "-B", branch, f"origin/{branch}")
        wt = Worktree(project, repo, path, branch, base, git(path, "rev-parse", "HEAD"), fresh=False, iid=iid)
        wt.notes += [f"已软链主仓库的 {n}" for n in link_deps(repo, path)]
        return wt

    base = c.get("base_branch") or "master"
    branch = f"{c.get('branch_prefix', 'agent/')}{task_id}"
    path = repo.parent / f"{repo.name}-agent-{_slug(task_id)}"
    git(repo, "fetch", "origin", base)
    if path.exists():  # 同一任务重试：复用
        return Worktree(project, repo, path, branch, base, git(path, "merge-base", "HEAD", f"origin/{base}"),
                        fresh=True)
    git(repo, "worktree", "add", "-b", branch, str(path), f"origin/{base}")
    wt = Worktree(project, repo, path, branch, base, git(path, "rev-parse", "HEAD"), fresh=True)
    wt.notes += [f"已软链主仓库的 {n}" for n in link_deps(repo, path)]
    return wt


SHARED_DEPS = ("node_modules",)


def link_deps(repo: Path, path: Path) -> list[str]:
    """把主仓库已装好的依赖（node_modules）软链进 worktree，Agent 才能跑 lint / 构建。
    只在链接后确认被 git 忽略时才保留，避免把软链提交上去。"""
    linked = []
    for name in SHARED_DEPS:
        src, dst = repo / name, path / name
        if not src.is_dir() or dst.exists() or dst.is_symlink():
            continue
        dst.symlink_to(src)
        if subprocess.run(["git", "check-ignore", "-q", name], cwd=str(path)).returncode == 0:
            linked.append(name)
        else:
            dst.unlink()
    return linked


def git_common_dir(wt: Worktree) -> Path:
    """worktree 的提交写在主仓库 .git 里：沙箱型 Agent（Codex workspace-write）要把它加进可写目录。"""
    p = Path(git(wt.path, "rev-parse", "--git-common-dir"))
    return p if p.is_absolute() else (wt.path / p).resolve()


def publish(cfg: dict, wt: Worktree, output: dict, agent: str, task_id: str, gl: GitLab | None = None) -> dict:
    """返回 {pushed, ahead, mr_url, dirty, removed}。"""
    c = _ci(cfg)
    gl = gl or GitLab(c.get("gitlab_url", "https://gitlab.example.com"))
    dirty = git(wt.path, "status", "--porcelain")
    ahead = int(git(wt.path, "rev-list", "--count", f"{wt.base_sha}..HEAD") or 0)
    res = {"pushed": False, "ahead": ahead, "mr_url": None, "dirty": bool(dirty), "removed": False}
    if ahead == 0:
        if wt.fresh and not dirty:
            git(wt.repo, "worktree", "remove", str(wt.path), check=False)
            if not wt.path.exists():
                git(wt.repo, "branch", "-D", wt.branch, check=False)
            res["removed"] = not wt.path.exists()
        return res
    git(wt.path, "push", "-u", "origin", wt.branch, timeout=180)
    res["pushed"] = True
    res["sha"] = git(wt.path, "rev-parse", "HEAD")
    if wt.iid:
        res["mr_url"] = (gl.mr(wt.project, wt.iid) or {}).get("web_url")
        res["iid"] = wt.iid
        return res
    existing = [m for m in gl.open_mrs(wt.project) if m.get("source_branch") == wt.branch]
    if existing:
        res["mr_url"] = existing[0].get("web_url")
        res["iid"] = existing[0].get("iid")
        return res
    title = (output.get("mr_title") or output.get("summary") or wt.branch).strip().splitlines()[0][:120]
    desc = (output.get("mr_description") or output.get("summary") or "").strip()
    desc += f"\n\n---\nproducer: {agent}\ntask: {task_id}\n由 Popto 产品运营研发多 Agent 系统开出；合并前需人批红。"
    mr = gl.create_mr(wt.project, source_branch=wt.branch, target_branch=wt.base, title=title, description=desc)
    res["mr_url"] = mr.get("web_url")
    res["iid"] = mr.get("iid")
    return res


def cleanup(wt: Worktree) -> None:
    """测试 / 人工清理用：删 worktree 目录和本地分支。"""
    git(wt.repo, "worktree", "remove", "--force", str(wt.path), check=False)
    if wt.path.exists():
        shutil.rmtree(wt.path, ignore_errors=True)
    git(wt.repo, "branch", "-D", wt.branch, check=False)
