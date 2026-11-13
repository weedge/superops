"""设置与策略。

- 策略写在仓库里的 policy.yaml（规则、各来源的名单 / 准入表 / 机器名单），机器相关的 ID 写在数据目录的 local.yaml
  （同名键深度覆盖）。改规则走 MR，和 decisions.md 一样有据可查。
- 运行时开关存在库里：暂停处理、接单模式（off / observe / auto）、单独暂停的规则 / Agent / 事件来源。
- 策略版本 = 规则 + 接单模式 + 各来源的策略部分 的哈希。每份授权都记着它是在哪个版本下给的；
  版本变了，进行中的评估作废重来，受影响的工单各自被告知。

settings_policy() 给出引擎用的 Policy：准入（谁可以发起工作、多强）、授权复核、谁能批红。
"""
from __future__ import annotations

import copy
import fnmatch
import re
from pathlib import Path
from typing import Any, Callable

import yaml

from ..biz import paths
from ..biz.timeutil import parse_duration
from ..core.errors import UserError
from ..core.model import now_iso
from ..core.reasons import key

POLICY_REVISION = "superops-mandate-v1"
MODES = ("off", "observe", "auto")
TELL = ("never", "needed", "always", "digest")
TOOLS = ("readonly", "workspace", "browser", "dev")
EXECUTORS = ("agent", "script", "none")
ADMISSION_DEFAULTS = {"listedUnaddressed": "inferred", "unlistedAddressed": "ignore", "unlistedUnaddressed": "ignore"}
ADMISSION_OPTIONS = {"listedUnaddressed": ("inferred", "ignore"), "unlistedAddressed": ("explicit", "inferred", "ignore"),
                     "unlistedUnaddressed": ("inferred", "ignore")}
STRENGTH = {"ignore": 0, "inferred": 1, "explicit": 2}
RULE_ID = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")


def deep_merge(base: dict, over: dict | None) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def same_name(a: str, b: str) -> bool:
    return str(a).strip().lower() == str(b).strip().lower()


def normalize_rule(raw: dict) -> dict:
    """把 policy.yaml 里的一条规则补全成固定形状；不合法就抛 ValueError。"""
    if not isinstance(raw, dict) or not RULE_ID.match(str(raw.get("id", ""))):
        raise ValueError(f"规则 id 不合法：{raw.get('id') if isinstance(raw, dict) else raw!r}")
    rid = raw["id"]
    unknown = set(raw) - {"id", "name", "enabled", "instruction", "when", "run", "tell", "priority", "debounce", "max_rounds",
                          "credits_per_run", "after", "after_max", "requires_approval", "idle_ttl", "note", "unless_live"}
    if unknown:
        raise ValueError(f"规则 {rid} 有不认识的键：{sorted(unknown)}")
    if not str(raw.get("instruction") or "").strip():
        raise ValueError(f"规则 {rid} 缺少 instruction（这条规则允许做什么，用人话写）")
    when = raw.get("when")
    if when is not None:
        if not isinstance(when, dict) or not when.get("types"):
            raise ValueError(f"规则 {rid} 的 when 需要 types")
        origin = when.get("origin", "*")
        types = [when["types"]] if isinstance(when["types"], str) else list(when["types"])
        if when.get("match"):
            try:
                re.compile(when["match"])
            except re.error as error:
                raise ValueError(f"规则 {rid} 的 when.match 不是合法的正则：{error}") from error
        when = {"origin": [origin] if isinstance(origin, str) else list(origin), "types": types,
                **({"match": when["match"]} if when.get("match") else {})}
    run = dict(raw.get("run") or {})
    run.setdefault("executor", "agent")
    if run["executor"] not in EXECUTORS:
        raise ValueError(f"规则 {rid} 的 run.executor 只能是 {EXECUTORS}")
    if run["executor"] == "script" and not run.get("command"):
        raise ValueError(f"规则 {rid} 是脚本执行器，需要 run.command")
    if run["executor"] == "agent":
        run.setdefault("role", "write")
        run.setdefault("tools", "workspace")
        if run["tools"] not in TOOLS:
            raise ValueError(f"规则 {rid} 的 run.tools 只能是 {TOOLS}")
        cw = run.get("conclude_with")
        if cw is not None and (not isinstance(cw, list) or not all(isinstance(t, str) and t for t in cw)):
            raise ValueError(f"规则 {rid} 的 run.conclude_with 要写成事件类型的列表")
    tell = raw.get("tell", "digest")
    if tell not in TELL:
        raise ValueError(f"规则 {rid} 的 tell 只能是 {TELL}")
    for field in ("debounce", "after_max", "idle_ttl"):
        if raw.get(field):
            parse_duration(raw[field])
    return {"id": rid, "name": str(raw.get("name") or rid), "enabled": bool(raw.get("enabled", True)),
            "instruction": str(raw["instruction"]).strip(), "when": when, "run": run, "tell": tell,
            "priority": int(raw.get("priority", 2)), "debounce": raw.get("debounce"), "max_rounds": raw.get("max_rounds"),
            "credits_per_run": raw.get("credits_per_run"), "after": list(raw.get("after") or []), "after_max": raw.get("after_max", "15m"),
            "requires_approval": bool(raw.get("requires_approval", False)), "idle_ttl": raw.get("idle_ttl"),
            "unless_live": list(raw.get("unless_live") or [])}


CHAT_APPS = ("dingtalk", "lark")   # 找人的聊天软件：钉钉和飞书只能开一个（2026-11-12 定）


def apply_chat(s: dict, chat: dict) -> None:
    """把面板上改的聊天渠道叠到设置上：群、审批人按来源替换；选了用哪个就只开它（另一个关掉），home 跟过去。"""
    for name, ids in (chat.get("chats") or {}).items():
        if name in CHAT_APPS:
            s["sources"].setdefault(name, {"enabled": False, "config": {}})["config"]["chats"] = [str(i) for i in ids]
    for name, people in (chat.get("approvers") or {}).items():
        if name in CHAT_APPS:
            part = s["policy"]["sources"].setdefault(name, {"allowed": [], "bots": [], "admission": {}, "answerers": {}})
            part["allowed"] = [{"id": str(p["id"]), **({"name": p["name"]} if p.get("name") else {})} for p in people if p.get("id")]
    app = chat.get("app")
    if app in CHAT_APPS:
        for name in CHAT_APPS:
            s["sources"].setdefault(name, {"enabled": False, "config": {}})["enabled"] = name == app
        groups = s["sources"][app]["config"].get("chats") or []
        if groups and (s["home"].get("source") in CHAT_APPS or s["home"].get("source") is None):
            s["home"] = {"source": app, "thread": "home", "address": {"chat": str(groups[0])}}


def normalize(raw: dict) -> dict:
    """policy.yaml（已合并 local.yaml）→ 固定形状的设置。"""
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ValueError("policy.yaml 顶层必须是一个映射")
    for name in ("eval", "executor", "sources", "policy", "home"):
        if raw.get(name) is not None and not isinstance(raw[name], dict):
            raise ValueError(f"{name} 必须是一个映射")
    for name in ("max_rounds", "credits_per_run"):
        for rule in (raw.get("policy") or {}).get("rules") or []:
            if isinstance(rule, dict) and rule.get(name) is not None and not isinstance(rule[name], (int, float)):
                raise ValueError(f"规则 {rule.get('id')} 的 {name} 必须是数字")
    ev = dict(raw.get("eval") or {})
    mode = ev.get("mode", "observe")
    if mode not in MODES:
        raise ValueError(f"eval.mode 只能是 {MODES}")
    ex = dict(raw.get("executor") or {})
    pol = raw.get("policy") or {}
    rules = [normalize_rule(r) for r in pol.get("rules") or []]
    ids = [r["id"] for r in rules]
    if len(set(ids)) != len(ids):
        raise ValueError("规则 id 重复：" + ", ".join(sorted({i for i in ids if ids.count(i) > 1})))
    for rule in rules:
        for field in ("after", "unless_live"):
            for dep in rule[field]:
                if dep not in ids:
                    raise ValueError(f"规则 {rule['id']} 的 {field} 指向不存在的规则 {dep}")
    sources_policy = {}
    for name, part in (pol.get("sources") or {}).items():
        part = part or {}
        admission = part.get("admission") or {}
        for row, cells in admission.items():
            for cell, value in (cells or {}).items():
                if cell not in ADMISSION_OPTIONS or value not in ADMISSION_OPTIONS[cell]:
                    raise ValueError(f"来源 {name} 的准入表 {row}.{cell} 不合法：{value}")
        entry = lambda e: {"id": str(e["id"] if isinstance(e, dict) else e), **({"name": e["name"]} if isinstance(e, dict) and e.get("name") else {})}
        sources_policy[name] = {"allowed": [entry(e) for e in part.get("allowed") or []],
                                "bots": [entry(e) for e in part.get("bots") or []], "admission": admission,
                                "answerers": {k: [str(i) for i in v or []] for k, v in (part.get("answerers") or {}).items()}}
    sources = {name: {"enabled": bool((cfg or {}).get("enabled", True)), "config": {k: v for k, v in (cfg or {}).items() if k != "enabled"}}
               for name, cfg in (raw.get("sources") or {}).items()}
    chat_apps = [n for n in CHAT_APPS if (sources.get(n) or {}).get("enabled")]
    if len(chat_apps) > 1:
        raise ValueError("钉钉和飞书只能二选一：sources.dingtalk.enabled 和 sources.lark.enabled 不能同时为 true")
    return {
        "paused": False,
        "paused_keys": {},
        "eval": {"mode": mode, "timeout_seconds": int(ev.get("timeout_seconds", 600)), "role": ev.get("role", "decide"),
                 "effort": ev.get("effort", "medium")},
        "executor": {"max_workers": int(ex.get("max_workers", 4)), "idle_ttl": ex.get("idle_ttl", "7d"),
                     "default": {"role": "write", "tools": "workspace", "timeout": "30m", **(ex.get("default") or {})}},
        "sources": sources,
        "home": raw.get("home") or {"source": "cli", "thread": "home", "address": {}},
        "policy": {"rules": rules, "sources": sources_policy},
    }


def policy_version(settings: dict) -> str:
    """只算影响“能不能开工”的部分；模型、并发、超时、暂停不让已有决定作废。"""
    rules = [{"id": r["id"], "enabled": r["enabled"], "instruction": r["instruction"], "when": r["when"],
              "executor": r["run"].get("executor"), "skill": r["run"].get("skill"), "command": r["run"].get("command"),
              "requires_approval": r["requires_approval"]} for r in settings["policy"]["rules"]]
    import json
    return key(POLICY_REVISION, json.dumps({"rules": rules, "mode": settings["eval"]["mode"], "sources": settings["policy"]["sources"]},
                                           ensure_ascii=False, sort_keys=True))


def rule_of(settings: dict, rule_id: str | None) -> dict | None:
    return next((r for r in settings["policy"]["rules"] if r["id"] == rule_id), None)


def trigger_event(trigger: dict) -> tuple[str, str, str]:
    """系统事件的 (类型, 事件来源, 对象)：定时和流水线事件把它们放在 meta 里。"""
    meta = trigger.get("meta") or {}
    return str(meta.get("type") or ""), str(meta.get("origin") or trigger["source"]), str(meta.get("subject") or "")


def rule_matches(rule: dict, trigger: dict) -> bool:
    when = rule.get("when")
    if not when:
        return False
    type_, origin, subject = trigger_event(trigger)
    if not type_:
        return False
    if "*" not in when["origin"] and origin not in when["origin"]:
        return False
    if not any(fnmatch.fnmatchcase(type_, pattern) for pattern in when["types"]):
        return False
    if when.get("match") and not re.search(when["match"], f"{subject}\n{trigger.get('text') or ''}"):
        return False
    return True


def matching_rules(settings: dict, trigger: dict, enabled_only: bool = True) -> list[dict]:
    return [r for r in settings["policy"]["rules"] if (r["enabled"] or not enabled_only) and rule_matches(r, trigger)]


def allowed_sender(settings: dict, actor: dict) -> dict | None:
    part = settings["policy"]["sources"].get(actor.get("source") or "") or {}
    return next((e for e in part.get("allowed") or [] if same_name(e["id"], actor.get("id", ""))), None)


def listed_bot(settings: dict, actor: dict) -> dict | None:
    part = settings["policy"]["sources"].get(actor.get("source") or "") or {}
    return next((e for e in part.get("bots") or [] if same_name(e["id"], actor.get("id", ""))), None)


def _admission(settings: dict, source: str, row: str | None, cell: str, system: bool = False) -> str:
    table = (settings["policy"]["sources"].get(source) or {}).get("admission") or {}
    value = (table.get(row) or {}).get(cell) if row is not None else None
    if value:
        return value
    # 系统来源（定时、流水线事件）没有发送人，默认作为推断候选进研判；人的会话默认忽略
    return "inferred" if system and cell == "unlistedUnaddressed" else ADMISSION_DEFAULTS[cell]


class SettingsPolicy:
    """引擎的 Policy 端口：每次用都从当前设置现做一个，自己不存状态。"""

    def __init__(self, settings: dict, sources):
        self.settings, self.sources = settings, sources
        self.version = policy_version(settings)
        self.paused = bool(settings.get("paused"))
        self.eval_timeout_ms = settings["eval"]["timeout_seconds"] * 1000
        self.mode = settings["eval"]["mode"]

    def _rules_on(self, rules: list[str] | None) -> bool:
        enabled = {r["id"] for r in self.settings["policy"]["rules"] if r["enabled"]}
        return all(i in enabled for i in rules or [])

    def _machine_gate(self, trigger: dict) -> dict:
        s, mode = self.settings, self.mode
        approved = (trigger.get("meta") or {}).get("approved")
        if approved and trigger["source"] == "pipeline":
            # 人在工单的问题上选中的选项发出的事件（凭据由宿主在批红时写入，Agent 写不进来）：这是人的明确决定，
            # 和名单内的人直接提出的请求一样，在任何接单模式下都有效。
            return {"admissible": True, "strength": "explicit",
                    "basis": {"source": trigger["source"], "scope": _scope_meta(trigger), "row": "events", "via": "approved",
                              "sender": approved.get("by"), "approved": {k: approved.get(k) for k in ("ticket", "option", "at")}}}
        bot = listed_bot(s, trigger["actor"]) if trigger["actor"].get("id") else None
        if trigger["actor"].get("id") and not bot:
            return {"admissible": False, "reason": "not_human"}
        scope = self.sources.scope(trigger["source"], trigger.get("meta") or {})
        if not scope["listened"] or not (trigger.get("addressed") or scope.get("observed")):
            return {"admissible": False, "reason": "not_human"}
        recorded = {"source": trigger["source"], "scope": _scope_meta(trigger), **({"row": scope["row"]} if scope.get("row") is not None else {})}
        addressed = {"addressed": trigger["addressed"]} if trigger.get("addressed") else {}
        if bot:
            if mode == "off":
                return {"admissible": False, "reason": "admission_ignored"}
            return {"admissible": True, "strength": "inferred",
                    "basis": {**recorded, "via": "bot", "sender": bot.get("name") or bot["id"], **addressed, "observation": {"mode": mode}}}
        cell = "unlistedUnaddressed"
        if _admission(s, trigger["source"], scope.get("row"), cell, bool(scope.get("system"))) != "inferred" or mode == "off":
            return {"admissible": False, "reason": "admission_ignored"}
        return {"admissible": True, "strength": "inferred", "basis": {**recorded, "via": "admission", "cell": cell, "observation": {"mode": mode}}}

    def gate(self, trigger: dict, thread: dict, tickets: list[dict]) -> dict:
        if not trigger["human"]:
            return self._machine_gate(trigger)
        s, mode = self.settings, self.mode
        allowed = allowed_sender(s, trigger["actor"])
        scope = self.sources.scope(trigger["source"], trigger.get("meta") or {})
        row = scope.get("row") if scope["listened"] else None
        recorded = {"source": trigger["source"], "scope": _scope_meta(trigger), **({"row": row} if row is not None else {}),
                    **({"sender": allowed.get("name") or allowed["id"]} if allowed else {})}
        addressed = trigger.get("addressed")
        if allowed and addressed:
            return {"admissible": True, "strength": "explicit", "basis": {**recorded, "via": "addressed", "addressed": addressed}}
        cell = "listedUnaddressed" if allowed else ("unlistedAddressed" if addressed else "unlistedUnaddressed")
        receivable = bool(addressed) or (scope["listened"] and bool(scope.get("observed")))
        admission = _admission(s, trigger["source"], row, cell) if receivable else "ignore"
        basis = {**recorded, "via": "admission", "cell": cell, **({"addressed": addressed} if addressed else {})}
        if admission == "explicit":
            return {"admissible": True, "strength": "explicit", "basis": basis}
        participating = any(i not in (thread.get("context") or []) for i in [*thread["subscriptions"], *[t["id"] for t in tickets]])
        if not (addressed == "direct" and not allowed) and (allowed or mode != "off") and participating:
            return {"admissible": True, "strength": "explicit" if allowed else "inferred", "basis": {**recorded, "via": "followup"}}
        if admission == "inferred" and mode != "off":
            return {"admissible": True, "strength": "inferred", "basis": {**basis, "observation": {"mode": mode}}}
        reason = "not_a_member" if not allowed else ("admission_ignored" if receivable and admission == "ignore" else "no_mention")
        return {"admissible": False, "reason": reason}

    def admits_inferred(self, rules: list[str]) -> bool:
        return self.mode == "auto" and bool(rules) and self._rules_on(rules)

    def revalidate(self, mandate: dict) -> bool:
        s, mode, basis = self.settings, self.mode, mandate["basis"]
        source = basis.get("source")
        current = self.sources.scope(source, basis.get("scope") or {}) if source else None
        if current and not current["listened"]:
            return False
        via = basis.get("via")
        if via == "approved":
            return True
        if via == "followup":
            return mandate["strength"] == "explicit" or (mode == "auto" and self._rules_on(basis.get("rules")))
        if via == "bot" and not listed_bot(s, mandate["actor"]):
            return False
        if via == "admission":
            cell = basis["cell"]
            listed = bool(allowed_sender(s, mandate["actor"]))
            if cell == "unlistedAddressed" and listed:
                return True
            now_cell = "listedUnaddressed" if cell == "unlistedUnaddressed" and listed else cell
            if now_cell == "listedUnaddressed" and not listed:
                return False
            row = current.get("row") if current and current["listened"] else None
            admission = _admission(s, source or "", row, now_cell, bool(current and current.get("system")))
            if STRENGTH[admission] < STRENGTH[mandate["strength"]]:
                return False
            if mandate["strength"] == "explicit":
                return True
        elif via != "bot" and not allowed_sender(s, mandate["actor"]):
            return False
        if via in ("admission", "bot") and basis.get("observation"):
            return mode == "auto" and self._rules_on(basis.get("rules"))
        return True

    def may_answer(self, trigger: dict, ticket: dict) -> bool:
        """全权审批人（来源名单上的人），或被限定只能批这条规则的人。"""
        if allowed_sender(self.settings, trigger["actor"]):
            return True
        part = self.settings["policy"]["sources"].get(trigger["actor"].get("source") or "") or {}
        rules = (ticket["mandate"].get("basis") or {}).get("rules") or []
        return any(any(same_name(trigger["actor"].get("id", ""), i) for i in (part.get("answerers") or {}).get(rule) or []) for rule in rules)


def _scope_meta(trigger: dict) -> dict:
    """授权里记下的接收范围：只留来源判断“还收不收”要用的几个键，不把整份事件数据抄进授权。"""
    meta = trigger.get("meta") or {}
    return {k: meta[k] for k in ("chat", "row", "type", "origin") if k in meta}


def policy_effects(tickets: list[dict], old: dict, now: dict, sources) -> dict[str, dict]:
    """设置从 old 变到 now，对每张未结束的工单的授权或命中的规则有什么影响。"""
    before, after = SettingsPolicy(old, sources), SettingsPolicy(now, sources)
    effects: dict[str, dict] = {}
    for ticket in tickets:
        rules = []
        for rid in (ticket["mandate"].get("basis") or {}).get("rules") or []:
            was, is_ = rule_of(old, rid), rule_of(now, rid)
            if not (is_ and is_["enabled"]):
                if was and was["enabled"]:
                    rules.append({"id": rid, "name": was["name"], "change": "disabled"})
            elif (was or {}).get("instruction") != is_["instruction"] or (was or {}).get("when") != is_["when"]:
                rules.append({"id": rid, "name": is_["name"], "change": "changed", "instruction": is_["instruction"]})
        effect: dict[str, Any] = {"rules": rules} if rules else {}
        if before.revalidate(ticket["mandate"]) and not after.revalidate(ticket["mandate"]):
            as_before = {**now, "eval": old["eval"], "policy": {**now["policy"], "rules": old["policy"]["rules"]}}
            if not SettingsPolicy(as_before, sources).revalidate(ticket["mandate"]):
                effect.update({"requesterAllowed": False, "requester": ticket["mandate"]["actor"]})
            elif ticket["mandate"]["strength"] == "inferred" and old["eval"]["mode"] == "auto" and now["eval"]["mode"] != "auto":
                effect["proactive"] = False
        if effect:
            effects[ticket["id"]] = effect
    return effects


class Settings:
    """读写设置：文件里的策略 + 库里的运行时开关。保存会算出策略版本的变化并告诉引擎。"""

    def __init__(self, store, sources, *, home: Path | None = None, on_change: Callable[[str, str, dict], None] | None = None):
        self.store, self.sources, self.home, self.on_change = store, sources, home or paths.ops_home(), on_change
        self._file: dict | None = None
        self._cache: dict | None = None

    def load_file(self) -> dict:
        base = yaml.safe_load((self.home / "policy.yaml").read_text(encoding="utf-8")) or {}
        local_path = paths.ops_data() / "local.yaml"
        local = (yaml.safe_load(local_path.read_text(encoding="utf-8")) or {}) if local_path.exists() else {}
        return normalize(deep_merge(base, {k: local[k] for k in ("eval", "executor", "sources", "policy", "home") if k in local}))

    def current(self) -> dict:
        if self._cache is None:
            if self._file is None:
                self._file = self.load_file()
            overrides = self.store.get("settings-overrides", {})
            s = copy.deepcopy(self._file)
            s["paused"] = bool(overrides.get("paused", False))
            s["paused_keys"] = dict(overrides.get("paused_keys") or {})
            if overrides.get("mode") in MODES:
                s["eval"]["mode"] = overrides["mode"]
            apply_chat(s, overrides.get("chat") or {})
            self._cache = s
        return self._cache

    def policy(self) -> SettingsPolicy:
        return SettingsPolicy(self.current(), self.sources)

    def start(self) -> None:
        """启动时：策略版本和上次不一样（比如停机期间改了 policy.yaml）就按一次策略变更处理。"""
        version = policy_version(self.current())
        last = self.store.get("policy-version")
        if last and last != version:
            self._record("file", last, version, {})
            if self.on_change:
                self.on_change(last, version, {})
        self.store.set("policy-version", version)

    def reload(self, origin: str = "file") -> dict:
        """重新读 policy.yaml / local.yaml；不合法就拒绝，保留原来的。"""
        try:
            new_file = self.load_file()
        except (ValueError, yaml.YAMLError, OSError, KeyError, TypeError, AttributeError) as error:
            raise UserError("invalidSettings", {"error": str(error)}) from error
        return self._apply(lambda: setattr(self, "_file", new_file), origin)

    def set_mode(self, mode: str, origin: str = "cli") -> dict:
        if mode not in MODES:
            raise UserError("invalidSettings", {"error": f"接单模式只能是 {MODES}"})
        return self._apply(lambda: self._override(mode=mode), origin)

    def pause(self, paused: bool, key_: str | None = None, reason: str = "", origin: str = "cli") -> dict:
        """key_ 为空 = 暂停 / 恢复全部处理；否则是 rule:<id> / agent:<名> / origin:<事件来源>。"""
        if key_ is None:
            return self._apply(lambda: self._override(paused=paused), origin)
        k = self.normalize_pause_key(key_)

        def change():
            keys = dict(self.store.get("settings-overrides", {}).get("paused_keys") or {})
            if paused:
                keys[k] = {"reason": reason, "at": now_iso()}
            else:
                keys.pop(k, None)
            self._override(paused_keys=keys)
        return self._apply(change, origin)

    def set_chat(self, *, app: str | None = None, chats: dict | None = None, approvers: dict | None = None,
                 origin: str = "panel") -> dict:
        """面板上改聊天渠道：用哪个（钉钉 / 飞书二选一）、各自的群、各自的审批人。存在库里的运行时覆盖，叠在 local.yaml 上。"""
        if app is not None and app not in CHAT_APPS:
            raise UserError("invalidSettings", {"error": f"聊天渠道只能是 {CHAT_APPS}"})

        def change():
            chat = dict(self.store.get("settings-overrides", {}).get("chat") or {})
            if app is not None:
                chat["app"] = app
            for field, value in (("chats", chats), ("approvers", approvers)):
                if value:
                    chat[field] = {**(chat.get(field) or {}), **value}
            self._override(chat=chat)
        return self._apply(change, origin)

    def normalize_pause_key(self, key_: str) -> str:
        if ":" in key_:
            return key_
        if rule_of(self.current(), key_) or any(r["name"] == key_ for r in self.current()["policy"]["rules"]):
            rule = rule_of(self.current(), key_) or next(r for r in self.current()["policy"]["rules"] if r["name"] == key_)
            return f"rule:{rule['id']}"
        return f"origin:{key_}"

    def is_paused(self, *keys: str) -> bool:
        paused = self.current().get("paused_keys") or {}
        return any(k in paused for k in keys)

    def history(self, limit: int = 20) -> list[dict]:
        revision = int(self.store.get("settings-revision", 0))
        return [v for v in (self.store.get(f"settings-version:{n}") for n in range(revision, max(0, revision - limit), -1)) if v]

    def _override(self, **changes) -> None:
        overrides = self.store.get("settings-overrides", {})
        overrides.update(changes)
        self.store.set("settings-overrides", overrides)

    def _apply(self, change: Callable[[], None], origin: str) -> dict:
        old = copy.deepcopy(self.current())
        old_version = policy_version(old)
        change()
        self._cache = None
        new = self.current()
        version = policy_version(new)
        result = {"policy_version": version, "changed": version != old_version, "mode": new["eval"]["mode"], "paused": new["paused"],
                  "paused_keys": sorted(new["paused_keys"])}
        if version != old_version:
            effects = policy_effects(self.store.tickets.open(), old, new, self.sources)
            self._record(origin, old_version, version, effects)
            self.store.set("policy-version", version)
            if self.on_change:
                self.on_change(old_version, version, effects)
            result["tickets_notified"] = sorted(effects)
        return result

    def _record(self, origin: str, old_version: str, version: str, effects: dict) -> None:
        revision = int(self.store.get("settings-revision", 0)) + 1
        self.store.set(f"settings-version:{revision}", {"revision": revision, "at": now_iso(), "origin": origin, "from": old_version,
                                                        "to": version, "tickets": sorted(effects)})
        self.store.set("settings-revision", revision)
