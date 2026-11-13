"""开发任务的 worktree：建分支、推送、开 MR（移植自 ops 的 tests/test_worktree.py）。"""
import subprocess

import pytest

from superops.biz.ci import worktree as wtm


def sh(cwd, *a):
    return subprocess.run(a, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class FakeGL:
    def __init__(self):
        self.created, self.mrs = [], {}

    def open_mrs(self, project):
        return [m for m in self.mrs.values()]

    def mr(self, project, iid):
        return self.mrs[iid]

    def create_mr(self, project, **kw):
        iid = len(self.mrs) + 1
        self.mrs[iid] = {"iid": iid, "web_url": f"https://gl/mr/{iid}", **kw}
        self.created.append(kw)
        return self.mrs[iid]


@pytest.fixture
def repo(tmp_path):
    remote = tmp_path / "remote.git"
    sh(tmp_path, "git", "init", "--bare", "-b", "master", str(remote))
    work = tmp_path / "proj" / "frontend_popto"
    work.parent.mkdir()
    sh(tmp_path, "git", "clone", str(remote), str(work))
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        sh(work, "git", "config", k, v)
    (work / "index.html").write_text('<html lang="en"></html>\n')
    sh(work, "git", "add", ".")
    sh(work, "git", "commit", "-m", "init")
    sh(work, "git", "push", "origin", "master")
    cfg = {"ci": {"projects": ["/front/x/popto"], "repos": {"front/x/popto": str(work)}, "base_branch": "master"}}
    return cfg, work, remote


def test_new_branch_commit_push_and_open_mr(repo):
    cfg, work, remote = repo
    gl = FakeGL()
    wt = wtm.prepare(cfg, "t_1", [{"data": {}}], gl=gl)
    assert wt.fresh and wt.branch == "agent/t_1" and wt.path == work.parent / "frontend_popto-agent-t_1"
    assert wt.project == "front/x/popto"
    (wt.path / "index.html").write_text('<html lang="zh-CN"></html>\n')
    sh(wt.path, "git", "commit", "-am", "fix lang")
    res = wtm.publish(cfg, wt, {"summary": "改语言", "mr_title": "页面语言改为 zh-CN", "mr_description": "说明"},
                      "claude", "t_1", gl=gl)
    assert res["pushed"] and res["mr_url"] == "https://gl/mr/1" and res["ahead"] == 1
    assert res["iid"] == 1 and res["sha"] == sh(wt.path, "git", "rev-parse", "HEAD")
    assert "agent/t_1" in sh(remote, "git", "branch")
    mr = gl.created[0]
    assert mr["target_branch"] == "master" and mr["title"] == "页面语言改为 zh-CN"
    assert "producer: claude" in mr["description"] and "task: t_1" in mr["description"]
    # 再跑一次不会重复开 MR
    gl.mrs[1]["source_branch"] = "agent/t_1"
    assert wtm.publish(cfg, wt, {"summary": "x"}, "claude", "t_1", gl=gl)["mr_url"] == "https://gl/mr/1"
    assert len(gl.created) == 1


def test_no_commit_removes_fresh_worktree(repo):
    cfg, work, _ = repo
    wt = wtm.prepare(cfg, "t_2", [], gl=FakeGL())
    res = wtm.publish(cfg, wt, {"summary": "只写了 PRD"}, "claude", "t_2", gl=FakeGL())
    assert res["removed"] and not wt.path.exists()
    assert "agent/t_2" not in sh(work, "git", "branch")


def test_fix_reuses_mr_branch(repo):
    cfg, work, remote = repo
    gl = FakeGL()
    wt = wtm.prepare(cfg, "t_3", [], gl=gl)
    (wt.path / "a.txt").write_text("1\n")
    sh(wt.path, "git", "add", ".")
    sh(wt.path, "git", "commit", "-m", "a")
    wtm.publish(cfg, wt, {"summary": "s", "mr_title": "t"}, "claude", "t_3", gl=gl)
    gl.mrs[1].update(source_branch="agent/t_3", target_branch="master")
    fix = wtm.prepare(cfg, "t_9", [{"data": {"project": "front/x/popto", "iid": 1}}], gl=gl)
    assert not fix.fresh and fix.branch == "agent/t_3" and fix.iid == 1
    (fix.path / "a.txt").write_text("2\n")
    sh(fix.path, "git", "commit", "-am", "fix")
    res = wtm.publish(cfg, fix, {"summary": "修了"}, "claude", "t_9", gl=gl)
    assert res["pushed"] and len(gl.created) == 1          # 修复只推送，不新开 MR
    assert sh(remote, "git", "log", "--format=%s", "-1", "agent/t_3") == "fix"


def test_missing_repo_mapping(repo):
    cfg, _, _ = repo
    cfg["ci"]["repos"] = {}
    with pytest.raises(wtm.WorktreeError, match="ci.repos"):
        wtm.prepare(cfg, "t_4", [], gl=FakeGL())


def test_node_modules_linked_and_ignored(repo):
    cfg, work, _ = repo
    (work / "node_modules" / "x").mkdir(parents=True)
    (work / ".gitignore").write_text("node_modules\n")
    sh(work, "git", "add", ".gitignore")
    sh(work, "git", "commit", "-m", "ignore")
    sh(work, "git", "push", "origin", "master")
    wt = wtm.prepare(cfg, "t_5", [], gl=FakeGL())
    assert (wt.path / "node_modules").is_symlink() and wt.notes
    assert sh(wt.path, "git", "status", "--porcelain") == ""        # 软链不算改动
    res = wtm.publish(cfg, wt, {"summary": "无改动"}, "claude", "t_5", gl=FakeGL())
    assert res["removed"] and not wt.path.exists()
    assert (work / "node_modules" / "x").is_dir()                     # 只删软链，不动主仓库依赖


def test_node_modules_not_linked_when_not_ignored(repo):
    cfg, work, _ = repo
    (work / "node_modules").mkdir()
    wt = wtm.prepare(cfg, "t_6", [], gl=FakeGL())
    assert not (wt.path / "node_modules").exists() and not wt.notes
