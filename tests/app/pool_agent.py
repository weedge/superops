#!/usr/bin/env python3
"""test_pool.py 用的假 Agent（agents.yaml 里 type: command）：`pool_agent.py <模式> <名字> <日志文件>`，提示词从 stdin 进。

模式：ok 正常输出；bad 输出不合 Schema；fix 第一次不合格、提示词里带了校验错误就改对；text 不输出 JSON；
file 把结果写到运行目录的 output.json；quota / quota30 / auth / network / crash 打印对应的错误后非零退出；sleep 一直睡。
"""
import json
import sys
import time

mode, name, log = sys.argv[1:4]
prompt = sys.stdin.read()
with open(log, "a", encoding="utf-8") as f:
    f.write(json.dumps({"agent": name, "mode": mode, "prompt": prompt}, ensure_ascii=False) + "\n")

ok = {"state": "ready", "summary": f"{name} 做完了"}
if mode == "ok":
    print(json.dumps(ok, ensure_ascii=False))
elif mode == "bad":
    print(json.dumps({"state": "nonsense"}))
elif mode == "fix":
    print(json.dumps(ok if "校验错误" in prompt else {"summary": 1}, ensure_ascii=False))
elif mode == "text":
    print("我做完了，但是没有按格式输出")
elif mode == "file":
    with open("output.json", "w", encoding="utf-8") as f:
        json.dump(ok, f, ensure_ascii=False)
    print("结果写在 output.json 里了")
elif mode == "sleep":
    time.sleep(60)
else:
    sys.stderr.write({
        "quota": "Error: You have hit your usage limit.\n",
        "quota30": "Error: usage limit reached, try again in 30 minutes\n",
        "auth": "Error: not logged in. Please run /login\n",
        "network": "request failed: connect ECONNREFUSED 127.0.0.1:443\n",
        "crash": "Traceback (most recent call last): boom\n",
    }[mode])
    sys.exit(1)
