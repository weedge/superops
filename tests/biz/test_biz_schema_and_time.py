"""Skill 输出 Schema、JSON 提取、时长 / 静默窗口、定时表、配置加载
（移植自 ops 的 tests/test_config_and_schema.py 和 test_dashboard.py 的 test_next_due；rules.json 校验没有了）。"""
import json
from datetime import datetime

import yaml

from superops.biz import config, scheduler, validate
from superops.biz.timeutil import TZ, in_window, iso, parse_duration, window_end

from bizhelpers import at


def test_schema_requires_card_when_needs_decision(home):
    sch = validate.build_schema(home / "skills" / "ops-daily-brief")
    out = {"status": "needs_decision", "summary": "x", "needs_decision": True, "message_markdown": "日报正文" * 5}
    errs = validate.validate(out, sch)
    assert any("decision_card" in e for e in errs)
    out["decision_card"] = {"title": "t", "proposal": "p", "options": [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}]}
    assert validate.validate(out, sch) == []
    del out["message_markdown"]
    assert any("message_markdown" in e for e in validate.validate(out, sch))


def test_all_skill_schemas_compile(home):
    from jsonschema import Draft202012Validator

    for d in (home / "skills").glob("ops-*"):
        Draft202012Validator.check_schema(validate.build_schema(d))


def test_extract_json():
    assert validate.extract_json('前言\n```json\n{"a": 1}\n```') == {"a": 1}
    assert validate.extract_json('结果：{"a": {"b": 2}} 完') == {"a": {"b": 2}}
    assert validate.extract_json("没有") is None


def test_durations_and_windows():
    assert parse_duration("10m").total_seconds() == 600
    assert parse_duration("2h").total_seconds() == 7200
    assert in_window(at("2026-11-10 23:00"), "22:30-08:00")
    assert in_window(at("2026-11-10 07:59"), "22:30-08:00")
    assert not in_window(at("2026-11-10 08:00"), "22:30-08:00")
    assert window_end(at("2026-11-10 23:00"), "22:30-08:00").strftime("%m-%d %H:%M") == "11-11 08:00"


def test_scheduler_last_due():
    now = at("2026-11-11 10:05")  # 周三
    assert scheduler.last_due({"at": "08:30"}, now).strftime("%m-%d %H:%M") == "11-11 08:30"
    assert scheduler.last_due({"at": "17:00"}, now).strftime("%m-%d %H:%M") == "11-10 17:00"
    assert scheduler.last_due({"at": "MON 09:00"}, now).strftime("%m-%d %H:%M") == "11-09 09:00"
    assert scheduler.last_due({"at": "WED 10:00"}, now).strftime("%m-%d %H:%M") == "11-11 10:00"
    assert scheduler.last_due({"at": "WED 11:00"}, now).strftime("%m-%d %H:%M") == "11-04 11:00"
    assert scheduler.last_due({"every": "30m"}, now).strftime("%H:%M") == "10:00"


def test_for_agent_schema_has_no_toplevel_combinators(home):
    sch = validate.for_agent(validate.build_schema(home / "skills" / "ops-comment-triage"))
    text = json.dumps(sch)
    assert "allOf" not in sch and "$ref" not in text and "$defs" not in sch
    assert sch["properties"]["signals"]["items"]["required"] == ["quote", "kind", "module", "intensity"]


def test_next_due():
    now = datetime(2026, 11, 10, 20, 10, tzinfo=TZ)  # 周二
    assert iso(scheduler.next_due({"every": "30m"}, now)) == "2026-11-10T20:30:00+08:00"
    assert iso(scheduler.next_due({"at": "08:30"}, now)) == "2026-11-11T08:30:00+08:00"
    assert iso(scheduler.next_due({"at": "17:00"}, now.replace(hour=9))) == "2026-11-10T17:00:00+08:00"
    assert iso(scheduler.next_due({"at": "WED 10:00"}, now)) == "2026-11-11T10:00:00+08:00"
    assert iso(scheduler.next_due({"at": "MON 09:00"}, now)) == "2026-11-16T09:00:00+08:00"


# ---------------------------------------------------------------- 配置加载（rules.json 相关的已删）
def test_agents_config_merges_local_yaml_over_repo(home, data_dir):
    cfg = config.load_agents_config(home)
    assert cfg["notifier"]["adapter"] == "file" and cfg["notifier"]["bot_name"] == "兔管家"   # local.yaml 覆盖，其余保留
    assert cfg["hub"]["port"] == 8797
    local = yaml.safe_load((data_dir / "local.yaml").read_text())
    local["hub"] = {"token": "s3cret"}
    (data_dir / "local.yaml").write_text(yaml.safe_dump(local, allow_unicode=True))
    hub = config.load_agents_config()["hub"]
    assert hub["token"] == "s3cret" and hub["host"] == "127.0.0.1"
    assert config.deep_merge({"a": {"b": 1, "c": [1]}}, {"a": {"c": [2]}, "d": 3}) == {"a": {"b": 1, "c": [2]}, "d": 3}


def test_schedules_load_and_every_entry_is_well_formed(home):
    sched = config.load_schedules(home)
    types = [s["type"] for s in sched]
    assert "cron.daily_brief" in types and "cron.data_retention" in types and len(types) == len(set(types))
    at_ = at("2026-11-10 10:00")
    for s in sched:
        assert "at" in s or "every" in s
        assert scheduler.last_due(s, at_) <= at_ < scheduler.next_due(s, at_)
        assert scheduler.max_late(s).total_seconds() > 0
    (home / "schedules.yaml").unlink()
    assert config.load_schedules(home) == []


def test_find_skill_prefers_repo_then_external_dirs(home, tmp_path):
    cfg = config.load_agents_config(home)
    assert config.find_skill("ops-daily-brief", cfg, home) == home / "skills" / "ops-daily-brief"
    ext = tmp_path / "ext-skills" / "my-skill"
    ext.mkdir(parents=True)
    (ext / "SKILL.md").write_text("x")
    assert config.find_skill("my-skill", {"external_skill_dirs": [str(tmp_path / "ext-skills")]}, home) == ext
    assert config.find_skill("ops-nope", cfg, home) is None
    assert config.CONTEXT_PROVIDERS >= {"hub_digest", "event_payload", "popto_help"} and "browser" in config.TOOLS
    assert "always" in config.TELL and "xhigh" in config.EFFORT


def test_every_context_provider_name_has_an_implementation():
    from superops.biz.context import ContextBuilder

    assert {n[2:] for n in dir(ContextBuilder) if n.startswith("p_")} == config.CONTEXT_PROVIDERS
