"""Agent 池（superops/executor/pool.py）：按降级链选 Agent、输出校验与重试、熔断与半开、排除 / 暂停 / 忙、用量策略、运行记录。

用 type: command 的假 Agent（tests/app/pool_agent.py）真的起子进程；适配器里的轮询间隔调短，时间（熔断到期）用 timeutil 的假时钟。
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from superops.app.effects import Later
from superops.app.health import HealthBoard
from superops.app.store import Store
from superops.biz import timeutil
from superops.executor import adapters
from superops.executor.pool import AgentPool

SCRIPT = str(Path(__file__).with_name("pool_agent.py"))
SCHEMA = {"type": "object", "required": ["state", "summary"],
          "properties": {"state": {"enum": ["ready", "waiting"]}, "summary": {"type": "string"}}}
T0 = datetime(2026, 11, 11, 10, 0, tzinfo=timeutil.TZ)


class FakeSettings:
    def __init__(self):
        self.paused: set[str] = set()

    def is_paused(self, *keys: str) -> bool:
        return any(k in self.paused for k in keys)


class Bench:
    """一个池子 + 它的假 Agent。agents: {名字: 模式}；模式 'missing' 表示本机没装（claude 类型、bin 不存在）。"""

    def __init__(self, tmp_path: Path, agents: dict[str, str], chain: list[str] | None = None, **cfg):
        self.tmp, self.log = tmp_path, tmp_path / "agents.log"
        self.store = Store(tmp_path / "db.sqlite")
        self.settings, self.health = FakeSettings(), HealthBoard()
        self.cfg = {"path": "", "agents": {name: self.agent(name, mode) for name, mode in agents.items()},
                    "fallback_chains": {"write": chain if chain is not None else list(agents)},
                    "circuit_breaker": {"quota_patterns": ["usage limit"], "auth_patterns": ["not logged in"], "network_patterns": ["econnrefused"]},
                    **cfg}
        self.pool = AgentPool(self.cfg, self.store, self.settings, self.health)
        self.prompts: list[tuple] = []
        self.n = 0

    def agent(self, name: str, mode: str, concurrency: int = 1) -> dict:
        if mode == "missing":
            return {"type": "claude", "bin": str(self.tmp / "not-installed" / name)}
        return {"type": "command", "bin": sys.executable, "args": [SCRIPT, mode, name, str(self.log)], "concurrency": concurrency}

    def become(self, name: str, mode: str) -> None:
        self.cfg["agents"][name]["args"][1] = mode

    def prompt(self, agent: str, errors: list[str] | None, fallback: bool) -> str:
        self.prompts.append((agent, errors, fallback))
        return "做这件事" + ("\n\n校验错误：" + "；".join(errors) if errors else "")

    def run(self, **kwargs):
        self.n += 1
        kwargs.setdefault("role", "write")
        kwargs.setdefault("timeout", 20.0)
        return self.pool.run(prompt=self.prompt, schema=SCHEMA, run_dir=self.tmp / "runs" / f"r{self.n}", **kwargs)

    def ran(self) -> list[str]:
        """各假 Agent 实际被拉起的顺序。"""
        if not self.log.exists():
            return []
        return [json.loads(line)["agent"] for line in self.log.read_text(encoding="utf-8").splitlines()]

    def rows(self, columns: str = "agent, status") -> list[tuple]:
        return self.store.all(f"SELECT {columns} FROM run ORDER BY rowid")

    def state(self, name: str) -> dict:
        return self.pool.state(name)


@pytest.fixture
def bench(tmp_path, monkeypatch):
    # 子进程的轮询间隔从 0.5 秒调到 20 毫秒，测试才跑得快；超时和租约的判断不变
    monkeypatch.setattr(adapters, "time", SimpleNamespace(monotonic=time.monotonic, sleep=lambda s: time.sleep(min(s, 0.02))))
    timeutil.set_fake_now(T0)
    made: list[Bench] = []

    def make(agents: dict[str, str], chain: list[str] | None = None, **cfg) -> Bench:
        b = Bench(tmp_path / f"b{len(made)}", agents, chain, **cfg)
        made.append(b)
        return b

    yield make
    timeutil.set_fake_now(None)
    for b in made:
        b.store.close()


def at(minutes: float) -> str:
    return timeutil.iso(T0 + timedelta(minutes=minutes))


# ---------------------------------------------------------------- 选谁


def test_按链的顺序用第一个可用的Agent(bench):
    b = bench({"a": "ok", "b": "ok"})
    out = b.run()
    assert (out.ok, out.agent, out.output, out.tried, out.is_fallback, out.cancelled) == (
        True, "a", {"state": "ready", "summary": "a 做完了"}, [], False, False)
    assert b.ran() == ["a"] and b.prompts == [("a", None, False)]
    assert b.pool.busy == {"a": 0}                                 # 跑完把位子还回去
    assert b.health.get("agent:a")["state"] == "ready"
    run_dir = b.tmp / "runs" / "r1"
    assert (run_dir / "turn-000-a.md").read_text(encoding="utf-8") == "做这件事"       # 提示词留档
    assert json.loads((run_dir / "schema.json").read_text(encoding="utf-8")) == SCHEMA


def test_prefer的Agent打头_fallback参数替换配置里的链(bench):
    b = bench({"a": "ok", "b": "ok", "c": "ok"}, chain=["a", "b"])
    assert b.pool.chain("write") == ["a", "b"]
    assert b.pool.chain("write", prefer="b") == ["b", "a"]
    assert b.pool.chain("write", prefer="c") == ["c", "a", "b"]
    assert b.pool.chain("write", fallback=["c"]) == ["c"] and b.pool.chain("write", prefer="b", fallback=["c", "b"]) == ["b", "c"]
    assert b.pool.chain("write", prefer="ghost", fallback=["ghost2", "a"]) == ["a"]     # 没配置的名字不算
    assert b.pool.chain("review") == []
    assert b.run(prefer="b").agent == "b" and b.run(fallback=["c"]).agent == "c"
    assert b.ran() == ["b", "c"]


def test_角色没有配置链时返回失败_不拉起任何Agent(bench):
    b = bench({"a": "ok"})
    out = b.run(role="review")
    assert (out.ok, out.error_kind, out.agent, out.tried) == (False, "no_agent", None, [])
    assert "review" in out.error_detail and b.ran() == [] and b.rows() == []


def test_Agent把结果写到运行目录的output_json也认(bench):
    b = bench({"a": "file"})
    out = b.run()
    assert out.ok and out.output == {"state": "ready", "summary": "a 做完了"}


# ---------------------------------------------------------------- 输出不合格


def test_输出不合格时同一个Agent带着校验错误重试一次(bench):
    b = bench({"a": "fix", "b": "ok"})
    out = b.run()
    assert (out.ok, out.agent, out.tried, out.is_fallback) == (True, "a", [], False)
    assert b.ran() == ["a", "a"]
    [(first, none, _), (second, errors, fallback)] = b.prompts
    assert (first, none, second, fallback) == ("a", None, "a", False)
    assert errors and any("state" in e for e in errors)             # 第二次的提示词带着具体的校验错误
    assert b.rows("agent, status, error_kind, is_fallback") == [("a", "invalid", "invalid", 0), ("a", "ok", None, 0)]
    assert "state" in b.rows("error_detail")[0][0]
    run_dir = b.tmp / "runs" / "r1"
    assert "校验错误" in (run_dir / "turn-001-a.md").read_text(encoding="utf-8")


def test_重试一次还不合格就换下一个_降级的Agent被告知自己是接手的(bench):
    b = bench({"a": "bad", "b": "ok"})
    out = b.run()
    assert (out.ok, out.agent, out.tried, out.is_fallback) == (True, "b", ["a"], True)
    assert b.ran() == ["a", "a", "b"]
    assert [(agent, bool(errors), fallback) for agent, errors, fallback in b.prompts] == [("a", False, False), ("a", True, False), ("b", False, True)]
    assert b.rows("agent, status, is_fallback") == [("a", "invalid", 0), ("a", "invalid", 0), ("b", "ok", 1)]
    assert b.state("a") == {"state": "closed"}                     # 输出不合格不熔断


def test_没有输出JSON也算不合格(bench):
    b = bench({"a": "text", "b": "ok"})
    out = b.run()
    assert out.agent == "b" and out.tried == ["a"]
    assert b.prompts[1][1] == ["没有拿到 JSON 输出"]


def test_链上的Agent都不合格_返回最后一个的失败和试过的名单(bench):
    b = bench({"a": "bad", "b": "text"})
    out = b.run()
    assert (out.ok, out.agent, out.error_kind, out.tried, out.output) == (False, "b", "invalid", ["a", "b"], None)
    assert out.error_detail.startswith("输出不符合要求：") and "没有拿到 JSON 输出" in out.error_detail
    assert b.ran() == ["a", "a", "b", "b"]


def test_check回调报的错误也触发重试_错误原样给到提示词(bench):
    b = bench({"a": "ok", "b": "ok"})
    seen = []

    def check(output: dict) -> list[str]:
        seen.append(output["summary"])
        return ["output.title 不能为空"] if len(seen) == 1 else []

    out = b.run(check=check)
    assert out.ok and out.agent == "a" and b.ran() == ["a", "a"]
    assert b.prompts[1] == ("a", ["output.title 不能为空"], False)
    assert b.rows("status, error_detail") == [("invalid", "output.title 不能为空"), ("ok", None)]


def test_check一直不过就换下一个_Schema都不过的输出不交给check(bench):
    b = bench({"a": "ok", "b": "bad"})
    checked = []

    def check(output: dict) -> list[str]:
        checked.append(output)
        return ["总是不行"]

    out = b.run(check=check)
    assert (out.ok, out.error_kind, out.tried) == (False, "invalid", ["a", "b"])
    assert len(checked) == 2                                       # 只有 a 的两次过了 Schema
    assert b.ran() == ["a", "a", "b", "b"]


# ---------------------------------------------------------------- 额度 / 认证 / 网络：熔断，立刻走下一个，不算试过


def test_额度错误熔断该Agent并立刻走下一个_不计入tried(bench):
    b = bench({"a": "quota", "b": "ok"})
    out = b.run()
    assert (out.ok, out.agent, out.tried, out.is_fallback) == (True, "b", [], True)
    assert b.ran() == ["a", "b"]                                   # 额度错误不重试
    state = b.state("a")
    assert (state["state"], state["kind"], state["open_until"], state["unavailable_since"]) == ("open", "quota", at(60), at(0))
    assert "usage limit" in state["last_error"]
    assert b.health.get("agent:a")["state"] == "waiting"
    assert b.rows("agent, status, error_kind") == [("a", "quota", "quota"), ("b", "ok", None)]
    # 熔断期间不再拉起它
    assert b.run().agent == "b" and b.ran() == ["a", "b", "b"]


def test_额度错误里写了恢复时间就熔断到那时_否则用配置的冷却时间(bench):
    b = bench({"a": "quota30", "b": "quota", "c": "ok"}, circuit_breaker={"quota_patterns": ["usage limit"], "quota_cooldown": "90m"})
    assert b.run().agent == "c"
    assert b.state("a")["open_until"] == at(30) and b.state("b")["open_until"] == at(90)


def test_认证错误熔断6小时_健康板报错(bench):
    b = bench({"a": "auth", "b": "ok"})
    out = b.run()
    assert (out.agent, out.tried) == ("b", [])
    assert (b.state("a")["state"], b.state("a")["kind"], b.state("a")["open_until"]) == ("open", "auth", at(360))
    assert b.health.get("agent:a")["state"] == "error" and [p["name"] for p in b.health.problems()] == ["agent:a"]


def test_网络错误冷却几分钟_不算试过(bench):
    b = bench({"a": "network", "b": "ok"})
    out = b.run()
    assert (out.agent, out.tried) == ("b", [])
    assert (b.state("a")["kind"], b.state("a")["open_until"]) == ("network", at(5))


def test_普通错误换下一个_算试过_不熔断(bench):
    b = bench({"a": "crash", "b": "ok"})
    out = b.run()
    assert (out.agent, out.tried, out.is_fallback) == ("b", ["a"], True)
    assert b.ran() == ["a", "b"] and b.state("a") == {"state": "closed"}
    assert b.rows("agent, status, error_kind") == [("a", "error", "error"), ("b", "ok", None)]
    assert "boom" in b.rows("error_detail")[0][0]


def test_唯一的Agent额度用完时排队等它恢复_不记失败(bench):
    b = bench({"a": "quota"})
    with pytest.raises(Later) as later:
        b.run()
    assert later.value.delay == 30 and "quota" in str(later.value) and "a" in str(later.value)
    with pytest.raises(Later):                                     # 熔断期间再来还是等，不再拉起它
        b.run()
    assert b.ran() == ["a"]


# ---------------------------------------------------------------- 半开


def test_冷却结束后半开_试成功就合上(bench):
    b = bench({"a": "quota", "b": "ok"})
    b.run()
    timeutil.set_fake_now(T0 + timedelta(minutes=59))
    assert b.pool.available("a") is False and b.run().agent == "b"
    timeutil.set_fake_now(T0 + timedelta(minutes=60))
    b.become("a", "ok")
    assert b.pool.available("a") is True and b.state("a")["state"] == "half_open"
    out = b.run()
    assert (out.agent, out.is_fallback) == ("a", False)
    assert b.state("a")["state"] == "closed" and "open_until" not in b.state("a") and "unavailable_since" not in b.state("a")
    assert b.health.get("agent:a")["state"] == "ready"
    assert b.ran() == ["a", "b", "b", "a"]


def test_半开时又失败就重新熔断_不可用的起点保持第一次的时间(bench):
    b = bench({"a": "quota", "b": "ok"})
    b.run()
    timeutil.set_fake_now(T0 + timedelta(minutes=61))
    out = b.run()                                                  # a 半开进去试，又是额度错误
    assert out.agent == "b" and b.ran() == ["a", "b", "a", "b"]
    state = b.state("a")
    assert (state["state"], state["open_until"], state["unavailable_since"]) == ("open", at(121), at(0))


# ---------------------------------------------------------------- 超时


def test_超时换下一个_算试过_记一次(bench):
    b = bench({"a": "sleep", "b": "ok"})
    out = b.run(timeout=1.0)
    assert (out.ok, out.agent, out.tried, out.is_fallback) == (True, "b", ["a"], True)
    assert b.state("a")["timeout_strikes"] == 1 and b.state("a").get("state") == "closed"
    assert b.rows("agent, status, error_kind") == [("a", "timeout", "timeout"), ("b", "ok", None)]
    assert b.ran() == ["a", "b"]                                   # 超时不在同一个 Agent 上重试


def test_同一个Agent连续3次超时熔断30分钟_之后不再拉起它(bench):
    b = bench({"a": "sleep"})
    for strikes in (1, 2):
        out = b.run(timeout=0.3)
        assert (out.ok, out.error_kind, out.tried, out.agent) == (False, "timeout", ["a"], "a")
        assert b.state("a")["timeout_strikes"] == strikes and b.state("a")["state"] == "closed"
    b.run(timeout=0.3)
    state = b.state("a")
    assert (state["state"], state["kind"], state["open_until"], state["timeout_strikes"]) == ("open", "timeout", at(30), 0)
    with pytest.raises(Later):
        b.run(timeout=0.3)
    assert b.ran() == ["a", "a", "a"]


def test_中间成功一次就把超时次数清零(bench):
    b = bench({"a": "sleep"})
    b.run(timeout=0.3)
    b.run(timeout=0.3)
    b.become("a", "ok")
    assert b.run().ok and b.state("a").get("timeout_strikes") is None
    b.become("a", "sleep")
    b.run(timeout=0.3)
    assert b.state("a")["timeout_strikes"] == 1 and b.state("a")["state"] == "closed"


def test_租约丢了_这一轮算取消_不熔断也不换人(bench):
    b = bench({"a": "sleep", "b": "ok"})
    out = b.run(alive=lambda: False)
    assert (out.ok, out.cancelled, out.error_kind, out.agent) == (False, True, "cancelled", "a")
    assert b.state("a") == {"state": "closed"} and b.prompts == [("a", None, False)]
    assert b.rows("agent, status") == [("a", "cancelled")] and b.pool.busy["a"] == 0


# ---------------------------------------------------------------- 排除、暂停、忙、没装


def test_exclude里的Agent不参与_审查者不能是生产者(bench):
    b = bench({"a": "ok", "b": "ok"})
    out = b.run(exclude=("a",))
    assert (out.agent, out.tried) == ("b", []) and b.ran() == ["b"]
    assert b.pool.chain("write", prefer="a", exclude=("a",)) == ["b"]          # 指名要它也不行
    nobody = b.run(exclude=("a", "b"))
    assert (nobody.ok, nobody.error_kind) == (False, "no_agent") and b.ran() == ["b"]


def test_被暂停的Agent跳过_恢复后照常(bench):
    b = bench({"a": "ok", "b": "ok"})
    b.settings.paused.add("agent:a")
    out = b.run()
    assert (out.agent, out.is_fallback) == ("b", True) and b.ran() == ["b"]
    assert b.pool.states()["a"]["paused"] is True and b.pool.states()["b"]["paused"] is False
    b.settings.paused.clear()
    assert b.run().agent == "a"


def test_链上的Agent都被暂停时排队等_不记失败(bench):
    b = bench({"a": "ok"})
    b.settings.paused.add("agent:a")
    with pytest.raises(Later):
        b.run()
    assert b.ran() == [] and b.rows() == []


def test_主力只是忙时先排队_等满overflow_after才顺延给下一个(bench):
    b = bench({"a": "ok", "b": "ok"})
    b.pool.busy["a"] = 1
    for waited in (0.0, 599.0):
        with pytest.raises(Later) as later:
            b.run(waited=waited)
        assert later.value.delay == 15 and "a" in str(later.value) and "忙" in str(later.value)
    assert b.ran() == [] and b.rows() == []
    out = b.run(waited=600.0)                                      # 默认等满 10 分钟
    assert (out.agent, out.is_fallback, out.tried) == ("b", True, [])
    assert b.pool.busy == {"a": 1, "b": 0}                         # 别人占的位子不动


def test_每多等一个overflow_after最多再往后顺延一位(bench):
    b = bench({"a": "ok", "b": "ok", "c": "ok"}, overflow_after="2m")
    b.pool.busy.update({"a": 1, "b": 1})
    with pytest.raises(Later) as first:
        b.run(waited=119.0)
    assert "a" in str(first.value)
    with pytest.raises(Later) as second:
        b.run(waited=120.0)                                        # 越过了 a，b 也在忙：再等一个周期
    assert "b" in str(second.value) and second.value.delay == 15
    assert b.ran() == []
    assert b.run(waited=240.0).agent == "c"


def test_overflow_after设为0时不等_忙就直接用下一个(bench):
    b = bench({"a": "ok", "b": "ok"}, overflow_after=0)
    b.pool.busy["a"] = 1
    out = b.run()
    assert (out.agent, out.is_fallback) == ("b", True)


def test_熔断和暂停的不用等_直接顺延(bench):
    b = bench({"a": "ok", "b": "ok", "c": "ok"})
    b.pool.open("a", "quota", "x")
    b.settings.paused.add("agent:b")
    assert b.run(waited=0.0).agent == "c"


def test_链上的Agent都在忙时抛Later_排队等(bench):
    b = bench({"a": "ok", "b": "ok"})
    b.pool.busy.update({"a": 1, "b": 1})
    with pytest.raises(Later) as later:
        b.run()
    assert later.value.delay == 15 and "忙" in str(later.value)
    with pytest.raises(Later) as waited_long:
        b.run(waited=3600.0)                                       # 等得再久，都在忙也只能接着等
    assert waited_long.value.delay == 30 and "忙" in str(waited_long.value)
    assert b.ran() == [] and b.rows() == [] and b.pool.busy == {"a": 1, "b": 1}
    b.pool.busy["a"] = 0
    assert b.run().agent == "a"


def test_并发数以内可以同时跑_跑的时候占着位子(bench):
    b = bench({"a": "ok"})
    b.cfg["agents"]["a"]["concurrency"] = 2
    b.pool.busy["a"] = 1
    seen = []
    original = b.prompt
    b.prompt = lambda agent, errors, fallback: seen.append(dict(b.pool.busy)) or original(agent, errors, fallback)
    assert b.run().agent == "a"
    assert seen == [{"a": 2}] and b.pool.busy == {"a": 1}


def test_拼提示词出错时位子也还回去(bench):
    b = bench({"a": "ok"})

    def broken(agent, errors, fallback):
        raise RuntimeError("提示词拼不出来")

    with pytest.raises(RuntimeError):
        b.pool.run(role="write", prompt=broken, schema=SCHEMA, run_dir=b.tmp / "runs" / "x")
    assert b.pool.busy == {"a": 0}


def test_链上没有本机装了的Agent时返回失败_不是无限等待(bench):
    b = bench({"claude": "missing", "codex": "missing"})
    assert b.pool.available("claude") is False and b.pool.states()["claude"]["installed"] is False
    out = b.run()
    assert (out.ok, out.error_kind, out.tried, out.agent) == (False, "no_agent", [], None)
    assert out.error_detail and b.rows() == []


def test_没装的跳过_装了的照常用(bench):
    b = bench({"claude": "missing", "b": "ok"})
    out = b.run()
    assert (out.agent, out.is_fallback) == ("b", True)


def test_装了的都失败过_剩下的没装_返回失败而不是等(bench):
    b = bench({"a": "crash", "claude": "missing"})
    out = b.run()
    assert (out.ok, out.error_kind, out.tried, out.agent) == (False, "error", ["a"], "a")
    assert "boom" in out.error_detail


def test_有Agent失败过但链上另一个只是暂时不可用_排队等那一个(bench):
    b = bench({"a": "crash", "b": "ok"})
    b.pool.busy["b"] = 1
    with pytest.raises(Later) as later:
        b.run()
    assert "b" in str(later.value)
    assert b.rows("agent, status") == [("a", "error")]              # 试过的那次仍然留了记录


def test_等别的Agent恢复期间_不重复拉起已经失败过的Agent(bench):
    b = bench({"a": "bad", "b": "quota"})
    tried: list[str] = []                                          # 调用方（工单执行器）按“派工 + 轮次”保存这份名单
    with pytest.raises(Later):
        b.run(tried=tried)
    assert b.ran() == ["a", "a", "b"] and tried == ["a"]
    for _ in range(3):                                             # 队列每 30 秒重来一次；b 还在熔断
        with pytest.raises(Later):
            b.run(tried=tried)
    assert b.ran() == ["a", "a", "b"]


# ---------------------------------------------------------------- 用量策略


def test_用量超线时_P2把它挪到链尾_P3不再用它_P0和P1不变(bench):
    b = bench({"claude": "ok", "codex": "ok", "agy": "ok"}, usage_policy={"claude": {"p2_threshold": 0.7, "p3_threshold": 0.5}})
    chain = lambda priority: b.pool.chain("write", priority=priority)
    assert [chain(p) for p in (0, 1, 2, 3)] == [["claude", "codex", "agy"]] * 4          # 没登记用量：都不调整
    b.pool.set_usage("claude", 0.49)
    assert [chain(p) for p in (0, 1, 2, 3)] == [["claude", "codex", "agy"]] * 4
    b.pool.set_usage("claude", 0.5)
    assert chain(3) == ["codex", "agy"] and chain(2) == ["claude", "codex", "agy"]
    b.pool.set_usage("claude", 0.7)
    assert chain(3) == ["codex", "agy"] and chain(2) == ["codex", "agy", "claude"]
    assert chain(1) == ["claude", "codex", "agy"] and chain(0) == ["claude", "codex", "agy"]
    assert b.pool.chain("write", prefer="claude", priority=2) == ["codex", "agy", "claude"]   # 规则指名要它也一样
    assert b.pool.chain("write", priority=4) == ["codex", "agy"]


def test_用量策略_只有它一个时P2仍然用它_没有策略的Agent不受影响(bench):
    b = bench({"claude": "ok", "codex": "ok"}, usage_policy={"claude": {"p2_threshold": 0.7, "p3_threshold": 0.5}})
    b.pool.set_usage("claude", 0.9)
    b.pool.set_usage("codex", 0.99)
    assert b.pool.chain("write", fallback=["claude"], priority=2) == ["claude"]
    assert b.pool.chain("write", fallback=["claude"], priority=3) == []
    assert b.pool.chain("write", fallback=["codex", "claude"], priority=3) == ["codex"]
    assert b.pool.chain("write", fallback=["codex"], priority=3) == ["codex"]


def test_用量超线后P2的任务由下一个Agent主跑_登记的用量在成功后保留(bench):
    b = bench({"claude": "ok", "codex": "ok"}, usage_policy={"claude": {"p2_threshold": 0.7, "p3_threshold": 0.5}})
    b.pool.set_usage("claude", 0.8)
    low = b.run(priority=2)
    assert (low.agent, low.is_fallback) == ("codex", False)
    assert b.run(priority=1).agent == "claude"
    assert b.state("claude")["usage_ratio"] == 0.8
    b.pool.open("claude", "network", "x")
    timeutil.set_fake_now(T0 + timedelta(minutes=6))
    assert b.run(priority=1).agent == "claude" and b.state("claude") == {"state": "closed", "usage_ratio": 0.8, "updated_at": at(6)}


# ---------------------------------------------------------------- 运行记录、状态


def test_每次拉起Agent都在run表留一行(bench):
    b = bench({"a": "fix", "b": "ok"})
    b.run(ticket="ticket-1", assignment="as-1", role="write")
    b.become("a", "crash")
    b.run(ticket="ticket-2", assignment="as-2")
    rows = b.rows("ticket, assignment, role, agent, is_fallback, status, error_kind")
    assert rows == [("ticket-1", "as-1", "write", "a", 0, "invalid", "invalid"), ("ticket-1", "as-1", "write", "a", 0, "ok", None),
                    ("ticket-2", "as-2", "write", "a", 0, "error", "error"), ("ticket-2", "as-2", "write", "b", 1, "ok", None)]
    for run in b.store.runs():
        assert run["id"].startswith("run_") and run["started_at"] == at(0) and run["finished_at"] == at(0)
        assert run["duration_s"] is not None and run["dir"].startswith(str(b.tmp / "runs"))
        assert (run["error_detail"] is None) == (run["status"] == "ok")
    assert len({r["id"] for r in b.store.runs()}) == 4
    assert [r["agent"] for r in b.store.runs("ticket-2")] in (["a", "b"], ["b", "a"])


def test_states列出每个Agent的熔断状态_占用_并发_是否安装_是否暂停(bench):
    b = bench({"a": "quota", "b": "ok", "claude": "missing"})
    b.cfg["agents"]["b"]["concurrency"] = 3
    b.settings.paused.add("agent:b")
    b.pool.busy["b"] = 2
    b.settings.paused.discard("agent:b")
    b.run()
    b.settings.paused.add("agent:b")
    states = b.pool.states()
    assert set(states) == {"a", "b", "claude"}
    assert (states["a"]["state"], states["a"]["kind"], states["a"]["busy"], states["a"]["concurrency"], states["a"]["installed"]) == ("open", "quota", 0, 1, True)
    assert (states["b"]["state"], states["b"]["busy"], states["b"]["concurrency"], states["b"]["paused"]) == ("closed", 2, 3, True)
    assert (states["claude"]["state"], states["claude"]["installed"], states["claude"]["paused"]) == ("closed", False, False)


def test_手动熔断_给了时长就用它(bench):
    b = bench({"a": "ok", "b": "ok"})
    b.pool.open("a", "network", "外部探测说它连不上", cooldown="10m")
    assert (b.state("a")["open_until"], b.state("a")["last_error"]) == (at(10), "外部探测说它连不上")
    assert b.run().agent == "b"
    timeutil.set_fake_now(T0 + timedelta(minutes=10))
    assert b.run().agent == "a"


# ---------------------------------------------------------------- 模型和思考强度


def test_模型和强度按Agent和角色取_规则的模型不串给降级的Agent(bench):
    b = bench({"a": "ok", "b": "ok"})
    b.cfg["agents"]["a"].update(models={"write": {"model": "m-a", "effort": "high"}}, default_effort="low")
    b.cfg["agents"]["b"].update(default_model="m-b", default_effort="medium")
    p = b.pool
    assert p.tuning("a", "write") == ("m-a", "high")
    assert p.tuning("a", "classify") == (None, "low")                                  # 没配这个角色：默认强度，模型交给 CLI
    assert p.tuning("a", "write", effort="max") == ("m-a", "max")                      # 规则特意定的强度优先
    assert p.tuning("a", "write", model="rule-m", owner="a") == ("rule-m", "high")
    assert p.tuning("b", "write", model="rule-m", owner="a") == ("m-b", "medium")      # 降级到 b：不把 a 的模型名带过去
    assert p.tuning("b", "write", model={"a": "x", "b": "y"}, owner="a") == ("y", "medium")
    assert b.run().ok and b.rows("agent, model, effort") == [("a", "m-a", "high")]   # 运行记录里记着实际用的
    assert p.states()["a"]["models"] == {"write": {"model": "m-a", "effort": "high"}}  # 面板运转图用


def test_旧库的运行记录表启动时补上模型和强度两列(tmp_path):
    import sqlite3
    db = tmp_path / "old.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE run (id TEXT PRIMARY KEY, ticket TEXT, assignment TEXT, role TEXT, agent TEXT, is_fallback INTEGER,"
                " started_at TEXT, finished_at TEXT, status TEXT, error_kind TEXT, error_detail TEXT, session_id TEXT, cost_usd REAL,"
                " duration_s REAL, dir TEXT)")
    con.execute("INSERT INTO run(id, agent, status) VALUES ('r0', 'claude', 'ok')")
    con.commit()
    con.close()
    store = Store(db)
    try:
        store.save_run({"id": "r1", "agent": "codex", "model": "gpt-x", "effort": "high", "status": "ok"})
        assert {(r["id"], r["model"], r["effort"]) for r in store.runs()} == {("r0", None, None), ("r1", "gpt-x", "high")}
    finally:
        store.close()
