#!/usr/bin/env python3
"""假的主 Session（type: command）：`route_agent.py <决定文件>`。把文件里的 decisions 原样输出，
其中 trigger_id 写成 "$TRIGGER" 的换成提示词里候选的那个；文件里是 "INVALID" 时输出一段不合格的东西。
test_housekeeping_assessor.py 用它给研判准备想要的输出。"""
import json
import re
import sys

prompt = sys.stdin.read()
body = prompt.split("## 待决定的候选", 1)[1] if "## 待决定的候选" in prompt else prompt
found = re.search(r"trigger_id: `([^`]+)`", body)
decisions = json.load(open(sys.argv[1], encoding="utf-8"))
if decisions == "INVALID":
    print("我觉得应该接，但我不按格式说")
    sys.exit(0)
for decision in decisions:
    if decision.get("trigger_id") == "$TRIGGER":
        decision["trigger_id"] = found.group(1) if found else "?"
print(json.dumps({"decisions": decisions}, ensure_ascii=False))
