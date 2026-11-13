"""Skill 输出 Schema：公共字段（03 §4.3）+ Skill 自己的 schema.json。"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from jsonschema import Draft202012Validator

OPTION = {
    "type": "object",
    "required": ["key", "label"],
    "properties": {
        "key": {"type": "string", "minLength": 1},
        "label": {"type": "string", "minLength": 1},
        "require_reason": {"type": "boolean"},
        "record": {"enum": ["not_doing", "decision", None]},
        "reply": {"type": "string"},
        "emit": {
            "type": "array",
            "items": {
                "type": "object", "required": ["type"],
                "properties": {"type": {"type": "string"}, "subject": {"type": ["string", "null"]},
                               "summary": {"type": ["string", "null"]}, "data": {"type": ["object", "null"]}},
            },
        },
    },
}

CARD = {
    "type": "object",
    "required": ["title", "proposal", "options"],
    "properties": {
        "kind": {"type": "string"},
        "title": {"type": "string", "minLength": 1, "maxLength": 80},
        "proposal": {"type": "string", "minLength": 1},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "items": {"type": "array", "items": {"type": "object"}},
        "options": {"type": "array", "minItems": 2, "maxItems": 4, "items": OPTION},
        "expires_at": {"type": "string"},
        "on_expire": {"type": ["string", "null"]},
        "links": {"type": "array", "items": {"type": "string"}},
    },
}

BASE_SCHEMA = {
    "type": "object",
    "required": ["status", "summary", "needs_decision"],
    "properties": {
        "status": {"enum": ["done", "needs_decision", "failed"]},
        "summary": {"type": "string", "minLength": 1, "maxLength": 600},
        "needs_decision": {"type": "boolean"},
        "decision_card": {"anyOf": [{"type": "null"}, CARD]},
        "message_markdown": {"type": "string"},
        "artifacts": {"type": "array", "items": {"type": "string"}},
        "follow_up_events": {
            "type": "array",
            "items": {
                "type": "object", "required": ["type"],
                "properties": {"type": {"type": "string", "pattern": r"^[a-z_]+(\.[a-z_]+)+$"},
                               "subject": {"type": ["string", "null"]}, "summary": {"type": ["string", "null"]},
                               "link": {"type": ["string", "null"]}, "data": {"type": ["object", "null"]}},
            },
        },
        "dogfood_friction": {
            "type": "array",
            "items": {"type": "object", "required": ["feature", "problem"],
                      "properties": {"feature": {"type": "string"}, "problem": {"type": "string"},
                                     "evidence": {"type": "string"}}},
        },
        "ops_feedback": {
            "type": "array",
            "items": {"type": "object", "required": ["problem"],
                      "properties": {"area": {"type": "string"}, "problem": {"type": "string"},
                                     "suggestion": {"type": "string"}}},
        },
        "credits_used": {"type": "number", "minimum": 0},
    },
    "allOf": [
        {"if": {"properties": {"needs_decision": {"const": True}}, "required": ["needs_decision"]},
         "then": {"required": ["decision_card"], "properties": {"decision_card": CARD}}},
    ],
}


def build_schema(skill_dir: Path | None) -> dict:
    schema = copy.deepcopy(BASE_SCHEMA)
    if skill_dir and (skill_dir / "schema.json").exists():
        extra = json.loads((skill_dir / "schema.json").read_text(encoding="utf-8"))
        schema["properties"].update(extra.get("properties") or {})
        if extra.get("$defs"):
            schema.setdefault("$defs", {}).update(extra["$defs"])
        schema["required"] = sorted(set(schema["required"]) | set(extra.get("required") or []))
        if extra.get("description"):
            schema["description"] = extra["description"]
    return schema


def for_agent(schema: dict) -> dict:
    """交给 Agent 结构化输出的 Schema：去掉顶层组合关键字（Claude 的结构化输出不支持），
    完整规则（如 needs_decision=true 时必须有卡片）仍由宿主校验。"""
    s = copy.deepcopy(schema)
    for k in ("allOf", "anyOf", "oneOf", "if", "then", "else", "$schema"):
        s.pop(k, None)
    defs = s.pop("$defs", {})

    def inline(node):
        if isinstance(node, dict):
            ref = node.get("$ref", "")
            if ref.startswith("#/$defs/") and ref[8:] in defs:
                return inline(copy.deepcopy(defs[ref[8:]]))
            return {k: inline(v) for k, v in node.items()}
        if isinstance(node, list):
            return [inline(v) for v in node]
        return node

    return inline(s)


def validate(output, schema: dict) -> list[str]:
    if not isinstance(output, dict):
        return ["输出不是 JSON 对象"]
    v = Draft202012Validator(schema)
    errs = []
    for e in sorted(v.iter_errors(output), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in e.path) or "(根)"
        errs.append(f"{loc}: {e.message[:300]}")
    return errs[:20]


_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def extract_json(text: str):
    """从 Agent 的最后一条消息里取出 JSON 对象。"""
    if not text:
        return None
    t = text.strip()
    try:
        v = json.loads(t)
        return v
    except json.JSONDecodeError:
        pass
    for m in _FENCE.finditer(t):
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
    i, j = t.find("{"), t.rfind("}")
    if i != -1 and j > i:
        try:
            return json.loads(t[i:j + 1])
        except json.JSONDecodeError:
            return None
    return None
