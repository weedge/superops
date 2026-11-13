"""确定性脚本（采集器 / 执行器 / CI）共用：读任务、发事件、输出结果。

被宿主以子进程拉起时，环境里有 OPS_TASK_FILE / OPS_RUN_DIR / OPS_HUB_URL；
stdout 的最后一行 JSON 就是任务结果（符合公共输出 Schema）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from . import config as cfgmod
from .client import HubClient


def load_task() -> dict:
    p = os.environ.get("OPS_TASK_FILE")
    if p and Path(p).exists():
        return json.loads(Path(p).read_text(encoding="utf-8"))
    return {"task": {"id": "manual"}, "rule": {}, "events": []}


def hub_client() -> HubClient:
    url = os.environ.get("OPS_HUB_URL")
    cfg = cfgmod.load_agents_config()
    h = cfg.get("hub") or {}
    # 被宿主拉起时环境里有 OPS_HUB_TOKEN：带着它发的事件才被当成可信的（确定性脚本发的）
    return HubClient(url or f"http://{h.get('host', '127.0.0.1')}:{h.get('port', 8797)}", os.environ.get("OPS_HUB_TOKEN") or h.get("token"))


def emit_result(summary: str, *, status: str = "done", needs_decision: bool = False, decision_card: dict | None = None,
                follow_up_events: list | None = None, message_markdown: str | None = None, **extra) -> dict:
    out = {"status": status, "summary": summary[:600] or "(空)", "needs_decision": needs_decision,
           "decision_card": decision_card, "follow_up_events": follow_up_events or [], "dogfood_friction": [], **extra}
    if message_markdown:
        out["message_markdown"] = message_markdown
    print(json.dumps(out, ensure_ascii=False, default=str))
    sys.stdout.flush()
    return out


def keychain(name: str) -> str | None:
    """密钥只放钥匙串：security add-generic-password -s popto-ops/<name> -a popto-ops -w <值>"""
    env = os.environ.get(f"OPS_SECRET_{name.upper().replace('-', '_')}")  # 仅测试用
    if env:
        return env
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", f"popto-ops/{name}", "-w"],
                           capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
