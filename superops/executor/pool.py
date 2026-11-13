"""Agent 池：本机有哪些 Agent、各自的熔断状态和并发、按角色的降级链，以及“按链跑一次并校验输出”。

规则沿用 ops：
- 额度 / 认证错误 → 熔断该 Agent（额度 60 分钟或解析出的恢复时间，认证 6 小时），任务立刻走下一个；
- 网络错误 → 冷却几分钟，不算试过；超时 → 换下一个，同一 Agent 连续 3 次超时熔断 30 分钟；
- 输出不合格 → 带着校验错误同一 Agent 重试 1 次，还不行再换；
- 主力只是忙（并发满）时先排队，等满 overflow_after（默认 10 分钟）才顺延给链上的下一个，每多等这么久最多再往后一位——
  不会因为一时忙就掉到最弱的 Agent；熔断、暂停、本机没有的不等，直接顺延；
- decide / dev / review 不降级到弱模型：链上没有可用的就等（抛 Later），不交给别的角色；
- 审查者 ≠ 生产者：exclude 里的 Agent 不参与。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Callable

from ..app.effects import Later
from ..biz import redact
from ..biz.timeutil import iso, now, parse_duration, parse_iso
from ..biz.validate import validate
from . import breaker
from .adapters import RunResult, RunSpec, adapter_for

log = logging.getLogger("superops.agents")


def agent_env(cfg: dict) -> dict:
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(os.path.expanduser(p) for p in str(cfg.get("path", "")).split(":") if p) or env.get("PATH", "")
    env["BROWSER"] = "/bin/false"
    proxy = cfg.get("proxy")
    if proxy:
        for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            env[k] = proxy
        env["NO_PROXY"] = env["no_proxy"] = cfg.get("no_proxy", "localhost,127.0.0.1")
    for k in list(env):  # 密钥不进 Agent 环境变量
        if any(s in k.upper() for s in ("TOKEN", "SECRET", "API_KEY", "PASSWORD")) and not k.startswith(("OPS_", "SUPEROPS_")):
            env.pop(k, None)
    return env


@dataclass
class Outcome:
    ok: bool
    output: dict | None = None
    agent: str | None = None
    session_id: str | None = None
    error_kind: str | None = None
    error_detail: str = ""
    tried: list[str] = field(default_factory=list)
    is_fallback: bool = False
    cancelled: bool = False


class AgentPool:
    def __init__(self, cfg: dict, store, settings, health=None):
        self.cfg, self.store, self.settings, self.health = cfg, store, settings, health
        self.busy: dict[str, int] = {}
        self.lock = threading.Lock()

    # ---- 状态 / 熔断

    def agents(self) -> dict:
        return self.cfg.get("agents") or {}

    def state(self, agent: str) -> dict:
        return self.store.get(f"agent:{agent}", {"state": "closed"})

    def states(self) -> dict[str, dict]:
        return {name: {**self.state(name), "busy": self.busy.get(name, 0), "concurrency": int(cfg.get("concurrency", 1)),
                       "title": cfg.get("title") or "", "duty": cfg.get("duty") or "",
                       "models": {role: {"model": t.get("model") or cfg.get("default_model"), "effort": t.get("effort") or cfg.get("default_effort")}
                                  for role, t in (cfg.get("models") or {}).items()},
                       "default_model": cfg.get("default_model"), "default_effort": cfg.get("default_effort"),
                       "installed": adapter_for(cfg).available(cfg), "paused": self.settings.is_paused(f"agent:{name}")}
                for name, cfg in self.agents().items()}

    def available(self, agent: str) -> bool:
        cfg = self.agents().get(agent)
        if not cfg or not adapter_for(cfg).available(cfg) or self.settings.is_paused(f"agent:{agent}"):
            return False
        st = self.state(agent)
        if st.get("state") == "open":
            if st.get("open_until") and parse_iso(st["open_until"]) <= now():
                self.store.set(f"agent:{agent}", {**st, "state": "half_open"})  # 冷却结束，放一个进去试
                return True
            return False
        return True

    def open(self, agent: str, kind: str, detail: str | None = None, *, cooldown: str | None = None, until: str | None = None) -> None:
        cb = self.cfg.get("circuit_breaker") or {}
        st = self.state(agent)
        default = {"quota": cb.get("quota_cooldown", "60m"), "auth": cb.get("auth_cooldown", "6h"),
                   "network": cb.get("network_cooldown", "5m"), "timeout": cb.get("timeout_cooldown", "30m")}.get(kind, "10m")
        open_until = until or iso(now() + parse_duration(cooldown or default))
        self.store.set(f"agent:{agent}", {**st, "state": "open", "kind": kind, "open_until": open_until, "last_error": (detail or "")[:500],
                                          "updated_at": iso(), "unavailable_since": st.get("unavailable_since") or iso()})
        if self.health:
            self.health.set(f"agent:{agent}", "error" if kind == "auth" else "waiting", f"{kind}：熔断到 {open_until}")

    def failed(self, agent: str, kind: str, detail: str | None, retry_at: str | None = None) -> None:
        cb = self.cfg.get("circuit_breaker") or {}
        if kind in ("quota", "auth", "network"):
            self.open(agent, kind, detail, until=retry_at if kind == "quota" else None)
        elif kind == "timeout":
            st = self.state(agent)
            strikes = int(st.get("timeout_strikes", 0)) + 1
            if strikes >= int(cb.get("timeout_strikes", 3)):
                self.open(agent, "timeout", detail)
                self.store.set(f"agent:{agent}", {**self.state(agent), "timeout_strikes": 0})
            else:
                self.store.set(f"agent:{agent}", {**st, "timeout_strikes": strikes, "last_error": (detail or "")[:500]})

    def succeeded(self, agent: str) -> None:
        st = self.state(agent)
        if st.get("state") != "closed" or st.get("timeout_strikes"):
            self.store.set(f"agent:{agent}", {"state": "closed", "usage_ratio": st.get("usage_ratio"), "updated_at": iso()})
        if self.health:
            self.health.set(f"agent:{agent}", "ready", "")

    def set_usage(self, agent: str, ratio: float) -> None:
        self.store.set(f"agent:{agent}", {**self.state(agent), "usage_ratio": ratio})

    # ---- 降级链

    def chain(self, role: str, *, prefer: str | None = None, fallback: list[str] | None = None, priority: int = 2,
              exclude: tuple[str, ...] = ()) -> list[str]:
        chain = [prefer] if prefer else []
        for a in (fallback if fallback is not None else (self.cfg.get("fallback_chains") or {}).get(role, [])):
            if a not in chain:
                chain.append(a)
        # 周用量超线时，低优先级任务不再用它打头
        for name, policy in (self.cfg.get("usage_policy") or {}).items():
            ratio = self.state(name).get("usage_ratio")
            if ratio is None or name not in chain:
                continue
            if priority >= 3 and ratio >= float(policy.get("p3_threshold", 1)):
                chain = [a for a in chain if a != name]
            elif priority == 2 and ratio >= float(policy.get("p2_threshold", 1)) and len(chain) > 1:
                chain = [a for a in chain if a != name] + [name]
        return [a for a in chain if a not in exclude and a in self.agents()]

    def _take(self, agent: str) -> bool:
        with self.lock:
            limit = int((self.agents().get(agent) or {}).get("concurrency", 1))
            if self.busy.get(agent, 0) >= limit:
                return False
            self.busy[agent] = self.busy.get(agent, 0) + 1
            return True

    def _release(self, agent: str) -> None:
        with self.lock:
            self.busy[agent] = max(0, self.busy.get(agent, 0) - 1)

    # ---- 模型和思考强度

    def tuning(self, agent: str, role: str, effort: str | None = None, model: str | dict | None = None,
               owner: str | None = None) -> tuple[str | None, str | None]:
        """这一轮实际用的模型和思考强度。
        模型：规则的 run.model（写成 {Agent: 模型} 按 Agent 取；写成字符串只给规则指定的那个 Agent，降级到别的 Agent 不带过去，
        免得把 Claude 的模型名传给 Codex）→ agents.<名>.models.<角色>.model → agents.<名>.default_model → CLI 自己的默认。
        强度：规则的 run.effort（这一类任务特意定的）→ agents.<名>.models.<角色>.effort → agents.<名>.default_effort。"""
        acfg = self.agents().get(agent) or {}
        per = (acfg.get("models") or {}).get(role) or {}
        if isinstance(model, dict):
            picked = model.get(agent)
        else:
            picked = model if model and owner in (None, agent) else None
        return (picked or per.get("model") or acfg.get("default_model") or None,
                effort or per.get("effort") or acfg.get("default_effort") or None)

    # ---- 跑一次

    def _idle_timeout(self, tools: str, acfg: dict) -> float:
        """浏览器档位的轮次：多久没有任何动作就提前结束（agent_browser.idle_timeout，默认 10m；0 关掉）。
        只对 Antigravity 和 Codex 生效：Codex 一直往标准输出写事件，Antigravity 的浏览器操作都落成运行目录里的页面快照；
        Claude 在 -p 模式下最后才输出、查网页也不写文件，套上会误杀，不启用。"""
        if tools != "browser" or acfg.get("type") not in ("antigravity", "codex"):
            return 0
        spec = (self.cfg.get("agent_browser") or {}).get("idle_timeout", "10m")
        return parse_duration(str(spec)).total_seconds() if spec not in (0, "0", None, "") else 0

    def run(self, *, role: str, prompt: Callable[[str, list[str] | None, bool], str], schema: dict, run_dir: Path, cwd: Path | None = None,
            tools: str = "readonly", effort: str | None = None, model: str | None = None, timeout: float = 900.0,
            prefer: str | None = None, fallback: list[str] | None = None, priority: int = 2, exclude: tuple[str, ...] = (),
            session: dict | None = None, alive: Callable[[], bool] = lambda: True, ticket: str | None = None,
            assignment: str | None = None, extra_dirs: list[Path] | None = None, env: dict | None = None,
            check: Callable[[dict], list[str]] | None = None, waited: float = 0.0,
            tried: list[str] | None = None, network: bool = False) -> Outcome:
        """按降级链找第一个可用且有空位的 Agent 跑一轮。prompt(agent, 校验错误, 是否降级) 现拼提示词。
        session: {agent: session_id}，同一个 Agent 接着它上一轮的会话。check 是 Schema 之外的额外校验。
        waited = 这一轮已经排了多久（秒），决定“主力在忙”时能不能顺延。
        链上的 Agent 都在忙或都熔断时抛 Later（任务排队等，不降级到别的角色）。"""
        chain = self.chain(role, prefer=prefer, fallback=fallback, priority=priority, exclude=exclude)
        if not chain:
            return Outcome(ok=False, error_kind="no_agent", error_detail=f"角色 {role} 没有配置可用的 Agent")
        tried = tried if tried is not None else []   # 调用方可以传入并保留：这一轮已经试过、失败了的 Agent 不再拉起
        run_dir.mkdir(parents=True, exist_ok=True)
        schema_path = run_dir / "schema.json"
        schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=1), encoding="utf-8")
        waiting = False
        last = Outcome(ok=False, error_kind="no_agent", error_detail="降级链上的 Agent 都不可用")
        overflow = parse_duration(self.cfg.get("overflow_after", "10m")).total_seconds()
        steps = int(waited // overflow) if overflow > 0 else len(chain)   # 等了这么久，允许越过几个“只是在忙”的 Agent
        for index, agent in enumerate(chain):
            if agent in tried or not self.available(agent):
                continue
            if not self._take(agent):
                waiting = True
                if steps <= 0:
                    raise Later(f"{agent} 在忙，先排队（等满 {self.cfg.get('overflow_after', '10m')} 才顺延给下一个）", 15)
                steps -= 1
                continue
            acfg = self.agents()[agent]
            if acfg.get("type") == "inbox" and acfg.get("max_wait"):
                timeout = parse_duration(acfg["max_wait"]).total_seconds()
            try:
                errors: list[str] | None = None
                for attempt in range(2):
                    (run_dir / "output.json").unlink(missing_ok=True)
                    text = prompt(agent, errors, index > 0)
                    n = len(list(run_dir.glob("turn-*.md")))
                    (run_dir / f"turn-{n:03d}-{agent}.md").write_text(text, encoding="utf-8")
                    use_model, use_effort = self.tuning(agent, role, effort, model, prefer)
                    spec = RunSpec(task_id=ticket or run_dir.name, run_dir=run_dir, cwd=cwd or run_dir, prompt=text, schema=schema,
                                   schema_path=schema_path, tools=tools, effort=use_effort, model=use_model, timeout=timeout, agent_cfg=acfg,
                                   env={**agent_env(self.cfg), **(env or {})}, alive=alive, extra_dirs=extra_dirs or [],
                                   session=(session or {}).get(agent), idle_timeout=self._idle_timeout(tools, acfg),
                                   network=network)
                    started, t0 = iso(), time.monotonic()
                    res: RunResult = adapter_for(acfg).run(spec, run_dir / f"turn-{n:03d}-{agent}.log")
                    if res.ok and res.output is not None:
                        res.output, _hits = redact.redact_obj(res.output)
                    errors = None
                    if res.ok:
                        errors = (validate(res.output, schema) if res.output is not None else ["没有拿到 JSON 输出"]) or None
                        if not errors and check:
                            errors = check(res.output) or None
                    kind = None
                    if not res.ok:
                        kind = "cancelled" if res.lease_lost else breaker.classify(res.error_text, timed_out=res.timed_out,
                                                                                   cb=self.cfg.get("circuit_breaker") or {})
                    elif errors:
                        kind = "invalid"
                    self.store.save_run({"id": f"run_{uuid.uuid4().hex[:12]}", "ticket": ticket, "assignment": assignment, "role": role,
                                         "agent": agent, "model": use_model, "effort": use_effort,
                                         "is_fallback": int(index > 0), "started_at": started, "finished_at": iso(),
                                         "status": "ok" if kind is None else kind, "error_kind": kind,
                                         "error_detail": ("；".join(errors) if errors else res.error_text)[:2000] if kind else None,
                                         "session_id": res.session_id, "cost_usd": res.cost_usd,
                                         "duration_s": round(time.monotonic() - t0, 1), "dir": str(run_dir)})
                    if kind is None:
                        self.succeeded(agent)
                        return Outcome(ok=True, output=res.output, agent=agent, session_id=res.session_id, tried=tried, is_fallback=index > 0)
                    if kind == "cancelled":
                        return Outcome(ok=False, agent=agent, error_kind="cancelled", tried=tried, cancelled=True)
                    if kind == "invalid" and attempt == 0:
                        log.info("%s 输出不合格，带着校验错误重试：%s", agent, (errors or [])[:3])
                        continue
                    detail = "输出不符合要求：" + "；".join(errors) if errors else res.error_text
                    last = Outcome(ok=False, agent=agent, error_kind=kind, error_detail=detail[:2000], tried=tried)
                    self.failed(agent, kind, detail, breaker.parse_retry_at(res.error_text) if kind == "quota" else None)
                    if kind not in ("network", "quota", "auth"):  # 这几类不算“试过”：恢复后还能接回来
                        tried.append(agent)
                    break
            finally:
                self._release(agent)
        # 链上还有没试过、本机装了的 Agent（只是在忙、熔断中或被暂停）：排队等它，不记失败
        remaining = [a for a in chain if a not in tried and adapter_for(self.agents()[a]).available(self.agents()[a])]
        if remaining:
            raise Later(f"角色 {role} 的 Agent 暂时不可用（{'忙' if waiting else last.error_kind}），排队等 {remaining[0]}", 30)
        if last.error_kind == "no_agent" and tried:
            last = Outcome(ok=False, error_kind="error", error_detail="降级链上的 Agent 都试过了，没有一个完成")
        last.tried = tried
        return last
