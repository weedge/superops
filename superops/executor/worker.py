"""工单执行器：引擎的 Executor 端口。每张工单一个目录、一段会话；每次有输入就跑“一轮”。

两种执行器，由工单命中的规则决定（明确请求没有规则，用默认的 Agent 执行器）：
- agent：拉起本机的 Claude / Codex / Antigravity（按角色的降级链），把工单卡片、Skill、相关决定、本轮输入拼成提示词，
  它的输出是一份结构化报告；宿主把报告落到引擎上（确认、交还、事件、问题、状态）。
- script：确定性脚本（采集器、发布 / 回复 / 合并执行器），不调用大模型；脚本的 decision_card 变成工单的问题卡片。

并发：同时运行的轮数不超过 executor.max_workers；人的消息可以多占一个位子，所以新工作挡不住人继续已有的工单。
"""
from __future__ import annotations

import json
import sys
import logging
import threading
import time
import uuid
from pathlib import Path

from jsonschema import Draft202012Validator

from ..app import prompts
from ..app.effects import Later
from ..app.settings import rule_of
from ..biz import agent_browser, config as bizconfig, context as bizcontext, governance, ingest, paths, redact, validate as bizvalidate
from ..biz.timeutil import iso, parse_duration
from ..core.errors import UserError
from ..core.model import iso_time, latest_report, live_ticket, ms, now_iso, running_assignment, shift, waits_for_person
from .adapters import RunSpec, adapter_for
from .pool import Outcome, agent_env

log = logging.getLogger("superops.executor")
_CARD = Draft202012Validator(bizvalidate.CARD)


# 告诉 Agent 这一轮的工具档位：无人值守时越权的工具调用不会弹窗询问，而是让整轮直接没有输出
TIER_NOTES = {
    # 两种 Agent 读文件的方式不一样：Claude 有 Read / Grep 工具，Bash 被禁；Codex 没有读文件工具，只能用 shell 命令读，
    # 只读沙箱允许读、拦写和联网。2026-11-12 以前这里写“不能运行任何命令”，Codex 照做后什么都读不了，合规复核连续 dropped
    "readonly": "当前是只读环境：只能读工作目录里的文件（context/、media/ 等），**不能写文件、不能联网，不能跑 ffprobe / python 这类程序**；越权会让这一轮直接失败。"
                "读文件：有 Read / Grep / Glob 这类读文件工具就用它们（这时 Bash 不可用，不要调）；没有读文件工具的（如 Codex）用 "
                "`cat`、`sed -n`、`head`、`ls`、`rg`、`jq` 这类只读命令读，这些是允许的。"
                "需要的材料宿主已经放在工作目录里，结果只写在最后一条消息的 JSON 里。",
    "workspace": "当前档位：可以读写工作目录，可以运行 python3 / uv / ffmpeg / ffprobe / node 等本地命令；不能联网、不能用 git、不能删除文件。",
    "browser": "当前档位：可以读写工作目录、运行本地命令，并通过宿主给的 `ops-browser` 操作专用浏览器；不要登录任何站点，不能用 git。",
    "dev": "当前档位：开发环境。可以改代码、跑检查和测试、`git commit`；不能 push、不能合并、不能强推、不能删除目录。",
}


def event_of(trigger: dict) -> dict:
    """触发 → 脚本和上下文提供者认识的事件形状（和 ops 的事件一样）。"""
    meta = trigger.get("meta") or {}
    return {"id": trigger["id"], "source": meta.get("origin") or trigger["source"], "type": meta.get("type") or f"{trigger['source']}.message",
            "subject": meta.get("subject"), "summary": trigger.get("text"), "link": trigger.get("url") or meta.get("link"),
            "payload_ref": meta.get("payload_ref"), "data": {**(meta.get("data") or {}), **({"approved": meta["approved"]} if meta.get("approved") else {})},
            "occurred_at": trigger["at"], "received_at": trigger["at"]}


def check_card(card: dict) -> list[str]:
    errors = [f"question.card{'.' + '.'.join(str(p) for p in e.path) if e.path else ''}: {e.message}" for e in _CARD.iter_errors(card)]
    if errors:
        return errors[:5]
    keys = [o["key"] for o in card["options"]]
    if len(set(keys)) != len(keys):
        return ["question.card.options 的 key 不能重复"]
    expire = card.get("on_expire")
    if expire:
        if not any(o["key"] == expire for o in card["options"]):
            return [f"question.card.on_expire={expire} 不是任何选项的 key"]
    return []


class TicketExecutor:
    def __init__(self, rt):
        self.rt = rt
        self.active: dict[str, threading.Event] = {}     # ticketId → 取消信号
        self.lock = threading.Lock()
        self.browser_lock = threading.Lock()
        paused = lambda: rt.settings.current()["paused"]
        workers = lambda: int(rt.settings.current()["executor"]["max_workers"])
        rt.effects.handle("launch", self._launch, limit=lambda: 0 if paused() else workers(), retry=(5.0, 300.0), quiet=True)
        rt.effects.handle("delivery", self._delivery, limit=lambda: 0 if paused() else workers() + 1, retry=(2.0, 60.0), quiet=True)

    # ---- 引擎端口（都只排队）

    def prepare(self, ticket: dict, outcome: dict) -> dict:
        """接单时：给工单编号、短名、目录，没有会话渠道的工单（定时、流水线起的）用 home 渠道找人。"""
        no = self.rt.store.next_no()
        slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in str(outcome.get("name") or "")).strip("-").lower()[:40]
        name = f"t{no}-{slug}" if slug else f"t{no}"
        ticket["no"] = no
        ticket["thread"] = {"source": "ticket", "id": f"T{no}"}
        ticket["execution"] = {"name": name, "directory": str(paths.ops_data() / "work" / name), "assignment": outcome.get("note") or ""}
        if not ticket["channels"]:
            rule = ((ticket["mandate"].get("basis") or {}).get("rules") or [None])[0]
            ticket["channels"] = [self.rt.home_channel(rule)]
        return ticket

    def start(self, ticket: dict, assignment: dict) -> None:
        self.rt.effects.enqueue("launch", assignment["id"], {"ticketId": ticket["id"], "assignmentId": assignment["id"]})

    def deliver(self, ticket: dict, assignment: dict) -> None:
        self.rt.effects.enqueue("delivery", ticket["id"], {"ticketId": ticket["id"]})

    def stop(self, ticket: dict, assignment: dict, reason: str) -> None:
        ticket_id, assignment_id = ticket["id"], assignment["id"]

        def cancel():
            self.rt.effects.cancel("launch", assignment_id)
            with self.lock:
                event = self.active.get(ticket_id)
            if event:
                event.set()
        self.rt.store.after_commit(cancel)

    def shutdown(self) -> None:
        """宿主停止：取消正在跑的轮（子进程整组杀掉），不留孤儿进程去写工单目录。重启后这些派工按“被打断”恢复。"""
        with self.lock:
            for event in self.active.values():
                event.set()

    def load(self) -> dict:
        with self.lock:
            return {"running": len(self.active), "max": int(self.rt.settings.current()["executor"]["max_workers"]), "tickets": sorted(self.active)}

    def resume(self) -> None:
        """启动时：上次还在跑的派工按“被打断”处理（最老的工单先来），待启动的重新排队。"""
        for ticket in sorted(self.rt.store.tickets.open(), key=lambda t: t["createdAt"]):
            running = running_assignment(ticket)
            if running:
                self.rt.engine.assignment_interrupted(ticket["id"], running["id"])
            else:
                latest = ticket["assignments"][-1] if ticket["assignments"] else None
                if latest and latest["state"] == "pending" and not waits_for_person(ticket):
                    self.start(ticket, latest)

    # ---- 副作用处理

    def _rule(self, ticket: dict) -> dict | None:
        rule_id = ((ticket["mandate"].get("basis") or {}).get("rules") or [None])[0]
        return rule_of(self.rt.settings.current(), rule_id) if rule_id else None

    def _launch(self, payload: dict, _key: str) -> None:
        ticket = self.rt.store.tickets.get(payload["ticketId"])
        assignment = next((a for a in (ticket or {}).get("assignments") or [] if a["id"] == payload["assignmentId"]), None)
        if not ticket or not live_ticket(ticket) or not assignment or assignment["state"] != "pending":
            return
        rule = self._rule(ticket)
        if rule and rule["after"]:  # 例如日报等采集跑完
            waited = (ms(now_iso()) - ms(assignment["createdAt"])) / 1000
            if waited < parse_duration(rule["after_max"]).total_seconds() and any(
                    t["id"] != ticket["id"] for dep in rule["after"] for t in self.rt.store.tickets_by_rule(dep) if live_ticket(t)):
                raise Later("等前置规则的工单跑完", 20)
        Path(ticket["execution"]["directory"]).mkdir(parents=True, exist_ok=True)
        self.rt.engine.assignment_started(ticket["id"], assignment["id"])
        self.rt.effects.enqueue("delivery", ticket["id"], {"ticketId": ticket["id"]})

    def _delivery(self, payload: dict, _key: str) -> None:
        ticket_id = payload["ticketId"]
        ticket = self.rt.store.tickets.get(ticket_id)
        assignment = running_assignment(ticket) if ticket else None
        if not ticket or not live_ticket(ticket) or not assignment:
            return
        turns = int(self.rt.store.get(f"turns:{assignment['id']}", 0))
        by_id = {i["id"]: i for i in ticket["inputs"]}
        inputs = [by_id[i] for i in ticket["undelivered"] if i in by_id]
        if turns and not inputs:
            return
        human = any(i["kind"] in ("trigger", "answer") and (i.get("human") or i["kind"] == "answer") for i in inputs)
        limit = int(self.rt.settings.current()["executor"]["max_workers"]) + (1 if human else 0)
        cancel = threading.Event()
        with self.lock:
            if ticket_id in self.active:
                raise Later("这张工单的上一轮还没结束", 2)
            if len(self.active) >= limit:
                raise Later("没有空的执行位", 5)
            self.active[ticket_id] = cancel
        rule = self._rule(ticket)
        run = (rule or {}).get("run") or self.rt.settings.current()["executor"]["default"]
        done = False
        try:
            if run.get("executor") == "script":
                done = self._script_turn(ticket, assignment, rule, run, inputs, turns, cancel)
            else:
                done = self._agent_turn(ticket, assignment, rule, run, inputs, turns, cancel)
        except Later:
            raise
        except UserError as error:
            log.info("工单 %s 这一轮的结果被引擎拒绝：%s", ticket_id[:8], error)
            done = True
        except Exception as error:  # noqa: BLE001
            log.exception("工单 %s 这一轮出错", ticket_id[:8])
            self._mark(ticket_id, assignment["id"], inputs)
            self._safe_report(ticket_id, {"state": "failed", "summary": f"执行器内部错误：{str(error)[:400]}"})
            done = True
        finally:
            with self.lock:
                self.active.pop(ticket_id, None)
            if done:
                self.rt.store.set(f"turns:{assignment['id']}", turns + 1)
                try:
                    self.rt.engine.turn_ended(ticket_id, assignment["id"])
                except UserError:
                    pass

    def _mark(self, ticket_id: str, assignment_id: str, inputs: list[dict], at: str | None = None) -> None:
        """这一轮确实跑了，才把输入记为已送达、已读（at = 这一轮开始、模型读到它们的时刻）。
        没跑成（Agent 都在忙、熔断中）的那次不记，输入留在队列里等下一次。"""
        ids = [i["id"] for i in inputs]
        if ids:
            self.rt.engine.inputs_delivered(ticket_id, assignment_id, ids)
            self.rt.engine.inputs_read(ticket_id, ids, at)

    def _safe_report(self, ticket_id: str, report: dict) -> dict | None:
        try:
            return self.rt.engine.report(ticket_id, report)
        except UserError as error:
            log.info("工单 %s 的报告没记上：%s", ticket_id[:8], error)
            return None

    def _lease(self, ticket: dict, rule: dict | None, run: dict, triggers: list[dict], turns: int) -> dict:
        return {"task": {"id": ticket["execution"]["name"], "ticket": ticket["id"], "attempts": turns, "round": len(ticket["assignments"]),
                         "rule": (rule or {}).get("name")},
                "rule": {"name": (rule or {}).get("name") or "明确请求", "id": (rule or {}).get("id"), "skill": run.get("skill"),
                         "tools": run.get("tools"), "capability": run.get("role"), "context": run.get("context") or []},
                "events": [event_of(t) for t in triggers]}

    def _triggers(self, ticket: dict, inputs: list[dict], first: bool) -> list[dict]:
        ids = [i["triggerId"] for i in inputs if i["kind"] == "trigger"]
        if first:
            ids = [*ticket["origin"]["triggerIds"], *ids]
        return [t for t in (self.rt.store.triggers.get(i) for i in dict.fromkeys(ids)) if t]

    # ---- 脚本执行器

    def _script_turn(self, ticket, assignment, rule, run, inputs, turns, cancel) -> bool:
        ticket_id = ticket["id"]
        self._mark(ticket_id, assignment["id"], inputs)
        if any(i["kind"] == "resumed" for i in inputs):
            # 已结束的脚本工单被人的消息恢复：脚本做的是确定性的（常常是对外的）动作，绝不因为一句话重跑。
            # 人的消息交还去评估，由主 Session 决定是不是新的工作。失败的脚本工单用“重试”重跑；已完成的要再跑，重新发它的触发。
            for item in inputs:
                if item["kind"] == "trigger" and item.get("human"):
                    try:
                        self.rt.engine.handback(ticket_id, item["triggerId"], "这是确定性脚本的工单，不处理人的消息")
                    except UserError:
                        pass
            self._safe_report(ticket_id, {"state": "ready", "summary": (latest_report(ticket) or {}).get("summary") or "脚本已执行过，没有重复执行"})
            return True
        if turns:  # 脚本只跑一次；之后只关心人对它卡片的回答
            answer = next((i for i in inputs if i["kind"] == "answer"), None)
            if answer:
                self._safe_report(ticket_id, {"state": "ready", "summary": f"{answer['by']} 选了「{answer.get('label') or answer['option']}」"})
            return True
        run_dir = paths.ops_data() / "runs" / ticket["execution"]["name"]
        run_dir.mkdir(parents=True, exist_ok=True)
        lease = self._lease(ticket, rule, run, self._triggers(ticket, inputs, True), turns)
        (run_dir / "task.json").write_text(json.dumps(lease, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        env = {**agent_env(self.rt.cfg), **self.rt.script_env(run_dir)}
        timeout = parse_duration(run.get("timeout") or "15m").total_seconds()
        spec = RunSpec(task_id=ticket_id, run_dir=run_dir, cwd=self.rt.home, prompt="", schema={}, schema_path=run_dir / "schema.json",
                       tools="workspace", effort=None, model=None, timeout=timeout, agent_cfg={"type": "script"}, env=env,
                       alive=lambda: not cancel.is_set(), command=run["command"])
        started, t0 = iso(), time.monotonic()
        res = adapter_for({"type": "script"}).run(spec, run_dir / "script.log")
        out = res.output if res.ok and isinstance(res.output, dict) else None
        status = "ok" if out is not None else ("cancelled" if res.lease_lost else "timeout" if res.timed_out else "error")
        self.rt.store.save_run({"id": f"run_{uuid.uuid4().hex[:12]}", "ticket": ticket_id, "assignment": assignment["id"], "role": "script",
                                "agent": "script", "is_fallback": 0, "started_at": started, "finished_at": iso(), "status": status,
                                "error_kind": None if out is not None else status, "error_detail": None if out is not None else res.error_text[:2000],
                                "duration_s": round(time.monotonic() - t0, 1), "dir": str(run_dir)})
        if res.lease_lost:
            return True
        if out is None:
            self._safe_report(ticket_id, {"state": "failed", "summary": ("脚本超时" if res.timed_out else "脚本失败") + f"：{res.error_text[-400:]}"})
            self._tell(ticket_id, rule, "failed", None)
            return True
        out, _ = redact.redact_obj(out)
        (run_dir / "output.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        self._emit(ticket, out.get("follow_up_events"), agent="script", trusted=True)
        if out.get("credits_used"):
            self.rt.add_counter("popto_credits", float(out["credits_used"]))
        summary = str(out.get("summary") or "(空)")[:1000]
        card = out.get("decision_card") if isinstance(out.get("decision_card"), dict) else None
        if out.get("status") == "failed":
            self._safe_report(ticket_id, {"state": "failed", "summary": summary})
            self._tell(ticket_id, rule, "failed", out.get("message_markdown"))
        elif (out.get("needs_decision") or out.get("status") == "needs_decision") and card:
            errors = check_card(card)
            if errors:  # 卡片发不出去就是失败：不能悄悄当成做完了
                self._safe_report(ticket_id, {"state": "failed", "summary": f"{summary}（脚本给的卡片不合规，没有发出：{'；'.join(errors)[:300]}）"})
                self._tell(ticket_id, rule, "failed", None)
            else:
                self._safe_report(ticket_id, {"state": "waiting", "summary": summary, "question": card["title"], "card": card,
                                              "expiresAt": self._expires(card)})
        else:
            self._safe_report(ticket_id, {"state": "ready", "summary": summary})
            self._tell(ticket_id, rule, "ready", out.get("message_markdown"))
        return True

    def _expires(self, card: dict, ttl: str | None = None) -> str:
        if card.get("expires_at"):
            return iso_time(card["expires_at"])  # 脚本写的是东八区时间；统一成 UTC 才能和“现在”比
        return shift(now_iso(), parse_duration(ttl or self.rt.cfg.get("card_default_ttl", "24h")).total_seconds())

    # ---- Agent 执行器

    def _agent_turn(self, ticket, assignment, rule, run, inputs, turns, cancel) -> bool:
        rt, ticket_id, first = self.rt, ticket["id"], turns == 0
        began = now_iso()
        work_dir = Path(ticket["execution"]["directory"])
        work_dir.mkdir(parents=True, exist_ok=True)
        role, tools = run.get("role", "write"), run.get("tools", "workspace")
        triggers = self._triggers(ticket, inputs, first)
        lease = self._lease(ticket, rule, run, triggers, turns)
        skill_dir = bizconfig.find_skill(run["skill"], rt.cfg, rt.home) if run.get("skill") else None
        if run.get("skill") and not skill_dir:
            self._mark(ticket_id, assignment["id"], inputs)
            self._safe_report(ticket_id, {"state": "failed", "summary": f"找不到 Skill {run['skill']}"})
            return True
        skill_schema = json.loads((skill_dir / "schema.json").read_text(encoding="utf-8")) if skill_dir and (skill_dir / "schema.json").exists() else None
        legacy_schema = bizvalidate.build_schema(skill_dir if skill_schema else None)
        notes: list[str] = [TIER_NOTES[tools]] if tools in TIER_NOTES else []
        ctx = bizcontext.ContextBuilder(rt.local_client, work_dir)
        wt, cwd, extra_dirs = None, work_dir, []
        browser = tools == "browser" and agent_browser.settings(rt.cfg)["enabled"]
        benv = {**agent_env(rt.cfg), "OPS_RUN_DIR": str(work_dir),
                "OPS_RENDER": f"{sys.executable} -m superops.biz.render_pages"}     # 图文出图：HTML 页面截成图
        if browser:
            self.browser_lock.acquire()
        try:
            providers = list(run.get("context") or [])
            if role == "video" and "event_media" not in providers:
                providers.append("event_media")
            try:
                ctx.build(providers, lease)
            except Exception as error:  # noqa: BLE001
                notes.append(f"部分上下文没准备出来：{str(error)[:200]}")
            if browser:
                error = agent_browser.open_session(rt.cfg, benv)
                if error:
                    self._mark(ticket_id, assignment["id"], inputs)
                    self._safe_report(ticket_id, {"state": "failed", "summary": f"专用浏览器没开起来，这一轮没有执行：{error}"[:600]})
                    return True
                notes.append(agent_browser.agent_note(rt.cfg))
            if run.get("worktree"):
                from ..biz.ci import worktree as wtmod
                try:
                    wt = wtmod.prepare(rt.cfg, ticket["execution"]["name"], lease["events"])
                except Exception as error:  # noqa: BLE001
                    self._mark(ticket_id, assignment["id"], inputs)
                    self._safe_report(ticket_id, {"state": "failed", "summary": f"开发环境没准备好：{str(error)[:400]}"})
                    return True
                cwd = wt.path
                extra_dirs.append(wtmod.git_common_dir(wt))
                notes.append(f"开发环境已准备好：当前目录是 `{wt.project}` 的独立 worktree（{wt.path}），分支 `{wt.branch}`，基于 `origin/{wt.base}`。"
                             "直接在这里改代码、跑检查、`git commit`。不要 push、不要开 MR、不要切分支：你这一轮结束后宿主会推送并开 MR，"
                             "标题和描述取你报告里的 `mr_title` / `mr_description`。")
            producers = tuple({(t.get("meta") or {}).get("data", {}).get("producer") for t in triggers} - {None}) if role == "review" else ()
            sessions = rt.store.get(f"session:{ticket_id}", {})
            skill_text = bizcontext.load_skill_text(skill_dir) if skill_dir else ""
            keywords = [ticket["title"], (rule or {}).get("name") or "", run.get("skill") or "", *[e.get("subject") or "" for e in lease["events"]]]
            decisions = governance.relevant_entries([k for k in keywords if k], home=rt.home)
            origin = rt.store.triggers.get(ticket["origin"]["triggerId"])

            prepared: list[str] = []

            def prompt(agent: str, errors: list[str] | None, fallback: bool) -> str:
                if role == "video" and (rt.cfg.get("agents") or {}).get(agent, {}).get("type") != "antigravity" and not prepared:
                    from ..biz import video_prep   # 不能直接看视频的 Agent：先抽帧、转写
                    prepared.extend(video_prep.prepare(work_dir) or ["视频已预处理成抽帧宫格和转写，见 context/"])
                extra = [*notes, *prepared, *(["你是降级接手的 Agent（主 Agent 不可用，或忙得排队太久）。"] if fallback else [])]
                compact = bool(sessions.get(agent)) and bool((rt.cfg.get("agents") or {}).get(agent, {}).get("resume")) and not first
                return prompts.worker_turn(rt.home, ticket=ticket, rule=rule, skill_text=skill_text, decisions=decisions,
                                           context_files="\n\n".join(x for x in (bizcontext.render_context_files(ctx),
                                                                                    bizcontext.render_untrusted(ctx.untrusted)) if x),
                                           inputs=inputs,
                                           triggers=rt.store.triggers.get, first=first, compact=compact, notes=extra, errors=errors,
                                           skill_schema=skill_schema, origin=origin)

            def check(out: dict) -> list[str]:
                errors: list[str] = []
                question = out.get("question")
                if question and out["state"] == "ready":
                    errors.append("有 question 时 state 不能是 ready")
                if question and question.get("card"):
                    errors += check_card(question["card"])
                if skill_schema and out["state"] in ("ready", "waiting") and (out.get("output") is not None or first):
                    errors += [f"output.{e}" for e in bizvalidate.validate(self._legacy(out), legacy_schema)][:6]
                return errors

            since = min([i["at"] for i in inputs] or [assignment["createdAt"]])
            tried_key = f"tried:{assignment['id']}:{turns}"
            tried: list[str] = rt.store.get(tried_key, [])
            try:
                outcome: Outcome = rt.agents.run(waited=max(0.0, (ms(began) - ms(since)) / 1000),
                    role=role, prompt=prompt, schema=prompts.REPORT_SCHEMA, run_dir=work_dir, cwd=work_dir if tools == "readonly" else cwd,
                    tools=tools, effort=run.get("effort"), model=run.get("model"), timeout=parse_duration(run.get("timeout") or "30m").total_seconds(),
                    prefer=run.get("agent"), fallback=run.get("fallback"), priority=(rule or {}).get("priority", 1), exclude=producers,
                    session=sessions, alive=lambda: not cancel.is_set(), ticket=ticket_id, assignment=assignment["id"], extra_dirs=extra_dirs,
                    env=rt.agent_dirs(work_dir) | ({"OPS_BROWSER_PREOPENED": "1"} if browser else {}), check=check, tried=tried,
                    network=bool(run.get("network")))
            except Later:
                rt.store.set(tried_key, tried)   # 排队等别的 Agent 恢复期间，不把已经失败过的再拉起来一遍
                raise
            rt.store.delete(tried_key)
        finally:
            ctx.close()
            if browser:
                try:
                    agent_browser.close(rt.cfg, benv)
                finally:
                    self.browser_lock.release()
            try:
                redact.scrub_tree(work_dir)  # 运行目录先打码：Cookie / token / 手机号不留在日志和产出里
            except Exception:  # noqa: BLE001
                log.exception("打码运行目录失败")
        if outcome.cancelled:
            return True
        self._mark(ticket_id, assignment["id"], inputs, began)
        if not outcome.ok:
            tried = "、".join(outcome.tried) or "无"
            self._safe_report(ticket_id, {"state": "failed", "summary": f"可用的 Agent 都没能完成这一轮（试过：{tried}）：{outcome.error_detail[:400]}"})
            self._tell(ticket_id, rule, "failed", None)
            return True
        if outcome.session_id:
            rt.store.set(f"session:{ticket_id}", {**sessions, outcome.agent: outcome.session_id})
        out = outcome.output
        host_events: list[dict] = []     # 宿主自己发的事件（推送后触发审查）；Agent 的输出进不了这个列表
        if wt is not None and out["state"] != "failed":
            host_events = self._publish(wt, out, outcome.agent, ticket)
        (work_dir / "output.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        self._apply(ticket, rule, run, out, outcome.agent, lease, skill_schema is not None, host_events)
        return True

    @staticmethod
    def _legacy(out: dict) -> dict:
        """把报告摆成 ops 公共输出的形状：Skill 的 schema.json 和入库程序（ingest）原样可用。"""
        question = out.get("question") or {}
        return {"status": "failed" if out["state"] == "failed" else ("needs_decision" if question else "done"), "report_state": out["state"],
                "summary": out["summary"][:600], "needs_decision": bool(question), "decision_card": question.get("card"),
                **({"message_markdown": out["message"]} if out.get("message") else {}),
                "follow_up_events": out.get("events") or [], "dogfood_friction": out.get("dogfood_friction") or [],
                "ops_feedback": out.get("ops_feedback") or [], "artifacts": out.get("artifacts") or [], **(out.get("output") or {})}

    def _publish(self, wt, out: dict, agent: str, ticket: dict) -> list[dict]:
        from ..biz.ci import worktree as wtmod
        legacy = {"summary": out["summary"], "mr_title": out.get("mr_title"), "mr_description": out.get("mr_description")}
        try:
            pub = wtmod.publish(self.rt.cfg, wt, legacy, agent, ticket["execution"]["name"])
        except Exception as error:  # noqa: BLE001
            out["state"], out["summary"] = "failed", f"{out['summary']}（代码已提交在 {wt.path}，但推送 / 开 MR 失败：{str(error)[:300]}）"[:600]
            return []
        if pub.get("pushed"):
            out["summary"] = f"{out['summary']}（已推送 {wt.branch}，MR：{pub['mr_url']}）"[:600]
            out["output"] = {**(out.get("output") or {}), "mr_url": pub["mr_url"], "branch": wt.branch}   # 入库时更新需求的 MR 链接
            if pub.get("iid"):
                subject = f"mr:{wt.project}!{pub['iid']}"
                self.rt.engine.bind_thread(ticket["id"], {"source": "pipeline", "id": subject}, True)  # 工单跟着自己的 MR，合并 / 关闭时结束
                return [{
                    "source": "ci", "type": "gitlab.mr.updated" if wt.iid else "gitlab.mr.opened", "subject": subject, "link": pub["mr_url"],
                    "summary": f"MR {wt.project}!{pub['iid']} 有新提交（{agent} 推送）", "dedup_key": f"mr:{wt.project}:{pub['iid']}:{pub['sha']}",
                    "data": {"project": wt.project, "iid": pub["iid"], "sha": pub["sha"], "producer": agent, "web_url": pub["mr_url"],
                             "title": out.get("mr_title")}}]
        elif pub.get("dirty"):
            out["summary"] = f"{out['summary']}（注意：{wt.path} 有未提交的改动，没有推送）"[:600]
        return []

    def _emit(self, ticket: dict, events, *, agent: str, trusted: bool) -> None:
        for index, event in enumerate(events or []):
            if not isinstance(event, dict) or not event.get("type"):
                continue
            event = {k: v for k, v in event.items() if not k.startswith("_")}
            event.setdefault("source", "pipeline")
            turn = self.rt.store.get(f"turns:{(ticket['assignments'][-1] or {}).get('id')}", 0)
            event.setdefault("dedup_key", f"follow:{ticket['id']}:{len(ticket['assignments'])}:{turn}:{index}:{event['type']}:{event.get('subject')}")
            data = dict(event.get("data") or {})
            data.setdefault("parent_ticket", ticket["id"])
            data.setdefault("parent_task", ticket["execution"]["name"])   # 工单目录名：下游按它找这张工单产出的文件
            event["data"] = data
            self.rt.pipeline.emit(event, by_ticket=ticket["id"], producer=agent, trusted=trusted)

    def _apply(self, ticket: dict, rule: dict | None, run: dict, out: dict, agent: str, lease: dict, has_schema: bool,
               host_events: list[dict] | None = None) -> None:
        """把 Agent 的报告落到引擎上。顺序：确认 → 交还 / 取消订阅 / 关注 → 入库 → 事件 → 报告 → 答复 → 结果。"""
        rt, ticket_id = self.rt, ticket["id"]

        def attempt(action, what: str):
            try:
                return action()
            except UserError as error:
                log.info("工单 %s 的%s被拒绝：%s", ticket_id[:8], what, error)
                return None

        for ack in out.get("ack") or []:
            attempt(lambda a=ack: rt.engine.reply({"purpose": "acknowledgement", "ticketId": ticket_id, "triggerId": a["trigger_id"], "text": a["text"]}), "确认")
        for item in out.get("handback") or []:
            attempt(lambda i=item: rt.engine.handback(ticket_id, i["trigger_id"], i["reason"]), "交还")
        for item in out.get("unrelated") or []:
            attempt(lambda i=item: rt.engine.mark_unrelated(ticket_id, i["thread"], i["reason"]), "取消订阅")
        for thread in out.get("follow") or []:
            if ":" in thread:
                source, _, tid = thread.partition(":")
                attempt(lambda s=source, t=tid: rt.engine.follow_for_context(ticket_id, {"source": s, "id": t}), "关注线程")
        bizcontext.normalize_script_paths({"scripts": (out.get("output") or {}).get("scripts"), "follow_up_events": out.get("events")},
                                          Path(ticket["execution"]["directory"]))
        # 入库在发事件之前：入库程序是确定性的，它可能自己追加事件（例如监听入库后发“今日线索卡片”），那些是宿主发的
        added: list[dict] = []
        if run.get("skill") and out["state"] != "failed" and (out.get("output") or has_schema):
            legacy = self._legacy(out)
            own = len(legacy["follow_up_events"])
            legacy["follow_up_events"] = list(legacy["follow_up_events"])
            try:
                ingest.apply(run["skill"], legacy, lease)
                added = legacy["follow_up_events"][own:]
            except Exception:  # noqa: BLE001
                log.exception("工单 %s 的产出入库失败", ticket_id[:8])
        self._emit(ticket, out.get("events"), agent=agent, trusted=False)
        self._emit(ticket, host_events, agent=agent, trusted=True)
        self._emit(ticket, [{**e, "dedup_key": e.get("dedup_key") or f"ingest:{ticket_id}:{i}:{e.get('type')}"} for i, e in enumerate(added)],
                   agent=agent, trusted=True)
        friction = [{"source": "worker", "type": "ops.dogfood.friction", "subject": f"feature:{f.get('feature') or '未知功能'}",
                     "summary": f"{f.get('feature') or '未知功能'}：{f.get('problem', '')}"[:300], "data": {**f, "ticket": ticket_id},
                     "dedup_key": f"friction:{ticket_id}:{f.get('feature')}:{str(f.get('problem', ''))[:60]}"}
                    for f in out.get("dogfood_friction") or [] if isinstance(f, dict)]
        feedback = [{"source": "worker", "type": "ops.system.feedback", "subject": f"ops:{f.get('area') or '其他'}",
                     "summary": f"{f.get('area') or '运营系统'}：{f['problem']}"[:300], "data": {**f, "ticket": ticket_id},
                     "dedup_key": f"opsfb:{f.get('area')}:{str(f['problem'])[:60]}"}
                    for f in out.get("ops_feedback") or [] if isinstance(f, dict) and f.get("problem")]
        for event in [*friction, *feedback]:
            rt.pipeline.emit(event, trusted=True)
        if out.get("credits_used"):
            rt.add_counter("popto_credits", float(out["credits_used"]))
        out = self._require_conclusion(ticket_id, run, out, host_events)
        state = "waiting" if out["state"] == "progress" else out["state"]
        report = {"state": state, "summary": out["summary"]}
        if "question" in out:
            question = out["question"]
            if question:
                report["question"] = question["text"]
                if question.get("asked_by"):
                    report["askedBy"] = question["asked_by"]
                if question.get("card"):
                    report["card"] = question["card"]
                    report["expiresAt"] = self._expires(question["card"], question.get("ttl"))
            else:
                report["question"] = None
        answer = out.get("answer")
        if answer and not out.get("question"):
            report["answers"] = answer["trigger_id"]
        try:
            rt.engine.report(ticket_id, report)
        except UserError as error:  # 指明的消息不对之类：去掉可选部分再记一次，报告本身不能丢
            log.info("工单 %s 的报告带的引用被拒绝（%s），去掉后重记", ticket_id[:8], error)
            for field in ("askedBy", "answers"):
                report.pop(field, None)
            self._safe_report(ticket_id, report)
        answered = False
        if answer and report.get("answers"):
            answered = bool(attempt(lambda: rt.engine.reply({"purpose": "answer", "ticketId": ticket_id, "triggerId": answer["trigger_id"],
                                                             "text": answer["text"]}), "答复"))
        # 答复已经把结果告诉了提问的人，就不再发一遍结果：同一件事只说一次
        if not (answered and state == "ready"):
            self._tell(ticket_id, rule, state, out.get("message"))

    def _require_conclusion(self, ticket_id: str, run: dict, out: dict, host_events: list[dict] | None) -> dict:
        """规则写了 run.conclude_with（例如合规复核的 通过 / 打回）：这张工单在某一轮发过其中一个事件才算有结论。
        没结论就报 ready / dropped 想结束的，改成 blocked 让人看到。2026-11-12 Codex 读不到脚本时把 10 张合规复核报成“无需处理”，
        脚本一条没审、也没人知道。带 question 的不拦（结论交给人选）。"""
        must = [str(t) for t in run.get("conclude_with") or []]
        if not must:
            return out
        flag = f"concluded:{ticket_id}"
        sent = {e.get("type") for e in [*(out.get("events") or []), *(host_events or [])] if isinstance(e, dict)}
        if sent & set(must):
            self.rt.store.set(flag, True)
            return out
        if out["state"] not in ("ready", "dropped") or out.get("question") or self.rt.store.get(flag):
            return out
        return {**out, "state": "blocked",
                "summary": f"没有给出结论就要结束（应在 events 里发 {' / '.join(must)} 之一），宿主改成受阻。Agent 的说法：{out['summary']}"[:600]}

    def _tell(self, ticket_id: str, rule: dict | None, state: str, message: str | None) -> None:
        """结果要不要告诉人：人直接提的请求总要回话；按规则开的工单看规则的 tell（always 才发结果，其余只在要人拍板时出声）。
        失败 / 受阻：明确请求或高优先级（P0 / P1）的才打扰人，其余进日报。"""
        rt = self.rt
        ticket = rt.store.tickets.get(ticket_id)
        if not ticket:
            return
        # “有人在等回话”只指人直接提的请求；人批准的事件开出的工单（发布、回复、写稿…）照规则的 tell 办
        explicit = ticket["mandate"]["strength"] == "explicit" and (ticket["mandate"].get("basis") or {}).get("via") != "approved"
        tell, priority = (rule or {}).get("tell", "always" if explicit else "digest"), (rule or {}).get("priority", 1 if explicit else 2)
        report = next((a["report"] for a in reversed(ticket["assignments"]) if a.get("report")), {"summary": ""})
        head = f"**T{ticket.get('no', '?')} · {ticket['title']}**"
        try:
            if state in ("ready", "dropped") and (explicit or tell == "always") and (state == "ready" or message):
                rt.engine.reply({"purpose": "result", "ticketId": ticket_id, "text": f"{head}\n\n{message or report['summary']}", "priority": priority})
            elif state in ("blocked", "failed") and (explicit or priority <= 1) and not ticket.get("attention"):
                label = "受阻，需要人处理" if state == "blocked" else "失败"
                rt.notify_ticket(ticket, f"{head} {label}\n\n{message or report['summary']}\n\n`superops ticket T{ticket.get('no')}` 查看，"
                                         f"`superops retry T{ticket.get('no')}` 重试，或直接回复这张工单继续。", priority=min(priority, 1))
        except UserError as error:
            log.info("工单 %s 的结果没发出去：%s", ticket_id[:8], error)
