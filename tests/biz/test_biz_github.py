"""GitHub 支持：gh api 客户端（返回形状和 GitLab 一样）、forge 按项目选 GitLab / GitHub、基线分支、MR / PR 链接匹配、
CI 轮询同时接两种、GitHub 仓库的开发 worktree。"""
from __future__ import annotations

import json
import subprocess

import pytest

from superops.biz.ci import forge
from superops.biz.ci.github import GitHub, GitHubError


class FakeGh:
    """假的 gh：按 (方法, 路径) 回 JSON，记下每次调用和请求体。"""

    def __init__(self, routes=None, auth_rc=0):
        self.routes, self.calls, self.auth_rc = dict(routes or {}), [], auth_rc

    def __call__(self, args, stdin=None):
        if args[:2] == ["auth", "status"]:
            return subprocess.CompletedProcess(args, self.auth_rc, stdout="", stderr="")
        method, path = args[args.index("-X") + 1], args[args.index("-X") + 2]
        body = json.loads(stdin) if stdin else None
        self.calls.append((method, path, body, "--paginate" in args))
        out = self.routes.get((method, path))
        if isinstance(out, Exception):
            return subprocess.CompletedProcess(args, 1, stdout="", stderr=str(out))
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(out if out is not None else {}), stderr="")


def pr(n, ref="agent/t1", sha="abc123", labels=(), merged_at=None, **kw):
    return {"number": n, "title": f"PR {n}", "body": "producer: codex", "head": {"ref": ref, "sha": sha}, "base": {"ref": "main"},
            "labels": [{"name": l} for l in labels], "html_url": f"https://github.com/o/r/pull/{n}", "state": "open",
            "merged_at": merged_at, "merge_commit_sha": "m123" if merged_at else None, **kw}


R = "repos/o/r"


def test_normalize_认几种写法():
    for s in ("o/r", "github:o/r", "https://github.com/o/r", "https://github.com/o/r.git", "git@github.com:o/r.git", "/o/r/"):
        assert GitHub.normalize(s) == "o/r", s


def test_PR的字段换成和GitLab一样的形状_合并过的才算merged():
    gh = FakeGh({("GET", f"{R}/pulls?state=open&per_page=50"): [pr(7, labels=["review:round-1"])],
                 ("GET", f"{R}/pulls?state=closed&sort=updated&direction=desc&per_page=20"): [
                     pr(5, merged_at="2026-11-15T10:00:00Z", merged_by={"login": "weedge"}), pr(4)]})
    g = GitHub(runner=gh)
    [m] = g.open_mrs("github:o/r")
    assert (m["iid"], m["source_branch"], m["target_branch"], m["sha"], m["labels"], m["web_url"]) == \
        (7, "agent/t1", "main", "abc123", ["review:round-1"], "https://github.com/o/r/pull/7")
    assert m["description"] == "producer: codex" and m["merge_commit_sha"] is None
    [merged] = g.merged_mrs("o/r")
    assert (merged["iid"], merged["merge_commit_sha"], merged["merged_by"]) == (5, "m123", {"username": "weedge"})


@pytest.mark.parametrize("runs,statuses,want", [
    ([], [], None),
    ([{"status": "completed", "conclusion": "success"}], [], "success"),
    ([{"status": "completed", "conclusion": "success"}], [{"state": "success"}], "success"),
    ([{"status": "completed", "conclusion": "failure"}], [], "failed"),
    ([], [{"state": "error"}], "failed"),
    ([{"status": "in_progress", "conclusion": None}], [], "running"),
    ([{"status": "completed", "conclusion": "skipped"}], [{"state": "pending"}], "running"),
])
def test_mr带上检查状态_没有检查等于没有流水线(runs, statuses, want):
    gh = FakeGh({("GET", f"{R}/pulls/7"): pr(7),
                 ("GET", f"{R}/commits/abc123/check-runs?per_page=100"): {"check_runs": runs},
                 ("GET", f"{R}/commits/abc123/status"): {"statuses": statuses}})
    assert GitHub(runner=gh).mr("o/r", 7)["head_pipeline"] == {"status": want}


def test_改动分页取全_形状同GitLab的changes():
    gh = FakeGh({("GET", f"{R}/pulls/7/files?per_page=100"): [
        [{"filename": "a.py", "status": "modified", "patch": "@@ -1 +1 @@\n-x\n+y"}],
        [{"filename": "b.png", "status": "removed"}, {"filename": "c.py", "previous_filename": "old.py", "status": "renamed"}]]})
    ch = GitHub(runner=gh).changes("o/r", 7)
    assert gh.calls[0][3] is True                                            # 带 --paginate --slurp
    assert [c["new_path"] for c in ch] == ["a.py", "b.png", "c.py"] and ch[0]["diff"].endswith("+y")
    assert ch[1]["deleted_file"] and ch[1]["diff"] == "" and ch[2]["old_path"] == "old.py"


def test_评论_标签_开PR_请求体对():
    gh = FakeGh({("POST", f"{R}/pulls"): pr(9, ref="agent/t9")})
    g = GitHub(runner=gh)
    g.note("o/r", 7, "审查意见")
    g.add_labels("o/r", 7, ["review:round-1", "review:passed"])
    m = g.create_mr("o/r", source_branch="agent/t9", target_branch="main", title="标题", description="说明")
    assert gh.calls[0][:3] == ("POST", f"{R}/issues/7/comments", {"body": "审查意见"})
    assert gh.calls[1][:3] == ("POST", f"{R}/issues/7/labels", {"labels": ["review:round-1", "review:passed"]})
    assert gh.calls[2][2] == {"title": "标题", "head": "agent/t9", "base": "main", "body": "说明"}
    assert (m["iid"], m["web_url"]) == (9, "https://github.com/o/r/pull/9")


def test_合并_按配置的方式_带sha_合完删源分支_没合上要报错():
    gh = FakeGh({("GET", f"{R}/pulls/7"): pr(7),
                 ("PUT", f"{R}/pulls/7/merge"): {"merged": True, "sha": "m999"}})
    res = GitHub(runner=gh, merge_method="squash").merge("o/r", 7, "abc123")
    assert res == {"merge_commit_sha": "m999", "web_url": "https://github.com/o/r/pull/7", "title": "PR 7"}
    assert ("PUT", f"{R}/pulls/7/merge", {"merge_method": "squash", "sha": "abc123"}, False) in gh.calls
    assert gh.calls[-1][:2] == ("DELETE", f"{R}/git/refs/heads/agent/t1")
    gh.routes[("PUT", f"{R}/pulls/7/merge")] = {"merged": False, "message": "Head branch was modified"}
    with pytest.raises(GitHubError, match="Head branch was modified"):
        GitHub(runner=gh).merge("o/r", 7)


def test_gh出错带上原因_没登录算没配好():
    gh = FakeGh({("GET", f"{R}/pulls/7"): RuntimeError("gh: Not Found (HTTP 404)")}, auth_rc=1)
    g = GitHub(runner=gh)
    with pytest.raises(GitHubError, match="HTTP 404"):
        g.mr("o/r", 7)
    assert g.configured is False and GitHub(runner=FakeGh()).configured is True


def test_forge_按写法分GitLab和GitHub_事件里写了forge优先():
    c = {"projects": ["front/x/popto", "github:o/r", "https://github.com/o/s.git"], "github_host": "github.com"}
    assert forge.projects(c) == [("gitlab", "front/x/popto"), ("github", "o/r"), ("github", "o/s")]
    assert forge.kind_of(c, "o/r") == "github" and forge.kind_of(c, "front/x/popto") == "gitlab"
    assert forge.kind_of(c, "x/y", {"forge": "github"}) == "github" and forge.kind_of(c, "x/y") == "gitlab"
    assert forge.kind_of({"default_project": "github:o/z"}, "o/z") == "github"
    assert forge.kind_of({"projects": ["https://ghe.corp/o/r"], "github_host": "ghe.corp"}, "o/r") == "github"
    assert isinstance(forge.client(c, "github"), GitHub)


def test_基线分支_按项目写的优先_GitHub用仓库默认分支_其余用全局或master():
    class Cl:
        def default_branch(self, project):
            return "main"
    c = {"projects": ["github:o/r", "g/p"], "base_branch": "develop"}
    assert forge.base_branch(c, "o/r", "github", Cl()) == "main"              # 全局的 develop 只管 GitLab
    assert forge.base_branch(c, "g/p", "gitlab") == "develop"
    assert forge.base_branch({"base_branch": {"github:o/r": "release"}}, "o/r", "github", Cl()) == "release"
    assert forge.base_branch({}, "g/p", "gitlab") == "master"


def test_需求池的MR和PR链接都认_号不会串():
    from superops.biz.ingest import mr_link_pattern
    links = "https://gl/front/x/popto/merge_requests/12,https://github.com/o/r/pull/3"
    assert mr_link_pattern("front/x/popto", 12).search(links) and not mr_link_pattern("front/x/popto", 1).search(links)
    assert mr_link_pattern("o/r", 3).search(links) and not mr_link_pattern("o/r", 31).search(links)
    assert mr_link_pattern("g/p", 5).search("https://gl/g/p/-/merge_requests/5")          # 新版 GitLab 的 /-/ 写法
    assert mr_link_pattern(None, 3).search(links) and not mr_link_pattern("o/x", 3).search(links)


def test_需求随PR合并标为done(home, data_dir):
    from superops.biz import db, ingest
    ops = db.open_ops()
    ops.execute("INSERT INTO demands(id, title, status, mr_links, updated_at) VALUES ('d1', '加导出', 'in_dev', ?, '2026-11-15')",
                ("https://github.com/o/r/pull/3",))
    ops.execute("INSERT INTO demands(id, title, status, mr_links, updated_at) VALUES ('d2', '别的', 'in_dev', ?, '2026-11-15')",
                ("https://github.com/o/r/pull/31",))
    ops.commit()
    assert ingest.demand_for_mr(ops, "o/r", 3)["id"] == "d1"
    ops.close()
    assert ingest.demand_merged("o/r", 3) == ["d1"]


def test_CI轮询同时接GitLab和GitHub_事件带forge_没登录的那种跳过并说明(monkeypatch):
    from superops.biz.ci import __main__ as ci

    class Fake:
        def __init__(self, kind, configured=True):
            self.kind, self.configured = kind, configured

        def open_mrs(self, project):
            return [{"iid": 3, "source_branch": "agent/t3", "sha": "s3", "labels": [], "title": "加导出", "web_url": f"https://{self.kind}/{project}/3"}]

        def merged_mrs(self, project):
            return []

    c = {"projects": ["g/p", "github:o/r"], "branch_prefix": "agent/", "verify": {}}
    out = {}
    monkeypatch.setattr(ci, "_ci_cfg", lambda: dict(c))
    monkeypatch.setattr(ci, "check_deploys", lambda gl=None, c=None: ([], []))
    monkeypatch.setattr(ci, "emit_result", lambda summary, **k: out.update(summary=summary, **k))
    monkeypatch.setattr(forge, "client", lambda cfg, kind: Fake(kind))
    ci.cmd_poll()
    by = {e["data"]["forge"]: e for e in out["follow_up_events"]}
    assert set(by) == {"gitlab", "github"} and by["github"]["subject"] == "mr:o/r!3"
    assert by["github"]["summary"].startswith("PR o/r!3") and by["gitlab"]["summary"].startswith("MR g/p!3")
    monkeypatch.setattr(forge, "client", lambda cfg, kind: Fake(kind, configured=kind == "gitlab"))
    ci.cmd_poll()
    assert {e["data"]["forge"] for e in out["follow_up_events"]} == {"gitlab"} and "gh 没登录 github.com" in out["summary"]


def test_GitHub仓库的worktree_从默认分支建_推送后开PR(tmp_path):
    from superops.biz.ci import worktree as wtm

    def sh(cwd, *a):
        return subprocess.run(a, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    remote = tmp_path / "remote.git"
    sh(tmp_path, "git", "init", "--bare", "-b", "main", str(remote))
    work = tmp_path / "r"
    sh(tmp_path, "git", "clone", str(remote), str(work))
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        sh(work, "git", "config", k, v)
    (work / "a.txt").write_text("x\n")
    sh(work, "git", "add", ".")
    sh(work, "git", "commit", "-m", "init")
    sh(work, "git", "push", "origin", "main")

    class FakeGH:
        created = []

        def default_branch(self, project):
            return "main"

        def open_mrs(self, project):
            return []

        def create_mr(self, project, **kw):
            self.created.append(kw)
            return {"iid": 1, "web_url": "https://github.com/o/r/pull/1"}

    cfg = {"ci": {"projects": ["github:o/r"], "repos": {"o/r": str(work)}, "base_branch": "master"}}
    gh = FakeGH()
    wt = wtm.prepare(cfg, "t_9", [], gl=gh)
    assert (wt.forge, wt.project, wt.base) == ("github", "o/r", "main")       # 全局 master 不管 GitHub 仓库
    (wt.path / "a.txt").write_text("y\n")
    sh(wt.path, "git", "commit", "-am", "改")
    res = wtm.publish(cfg, wt, {"summary": "改", "mr_title": "改 a"}, "claude", "t_9", gl=gh)
    assert res["forge"] == "github" and res["mr_url"] == "https://github.com/o/r/pull/1"
    assert gh.created[0]["target_branch"] == "main" and gh.created[0]["source_branch"] == "agent/t_9"
