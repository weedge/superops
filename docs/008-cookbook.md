# 008 扩展手册

这篇讲怎么扩展 superops：加一条规则、加一种来源、加一个 Skill、加一个上下文提供者。用的词来自 [001 领域模型](001-domain.md) 和 [002 授权](002-authorization.md)，部件来自 [007 架构](007-architecture.md)。

先问一句要不要加。按 [000 理念](000-philosophy.md)，一个业务需求先试着变成已有概念的属性：多数“新功能”是一条规则加一个 Skill，不需要新的来源，更不需要改引擎。

## 加一条规则

规则写在 `policy.yaml` 的 `policy.rules`，键的含义见 002。三种执行方式各举一例。每加一条：`uv run superops check` 校验，`superops reload` 生效；`observe` 模式下先看它“本来会接”什么。

### Agent 来做：每周的竞品动态

```yaml
# policy.yaml
- id: competitor-brief
  name: 竞品动态周报
  instruction: 每周四汇总监听到的竞品动态，写一页周报发到运营群。只读取和汇总，不做任何对外动作。
  when: {origin: cron, types: [cron.competitor_brief]}
  run: {role: write, agent: claude, skill: ops-competitor-brief, tools: workspace, effort: medium, timeout: 20m,
        context: [hub_digest_week, listening_terms]}
  tell: always
```

```yaml
# schedules.yaml
- { type: cron.competitor_brief, at: "THU 10:00" }
```

- `instruction` 是这张工单的范围，原样给执行器。写清允许做什么、不允许做什么，一两句。
- `when.origin: cron` 只认定时来源的事件；不写 `origin` 就是任何来源的这个类型。
- `role` 选降级链（`agents.yaml` 的 `fallback_chains`），`agent` 指定打头的那个。
- `tools` 选最低够用的档位。读评论、帖子等外部内容的规则用 `readonly`。
- `tell: always` 让结果发到群里。它是会打扰人的选择：全部规则里最多 4 条，`superops check` 会拦。拿不准就用 `needed`（只在要人拍板时出声）。
- Skill 怎么写见下面“加一个 Skill”。

### 脚本来做：每天导出线索

确定的工作不用大模型。脚本读任务、干活、在 stdout 最后一行输出结果：

```python
"""python -m superops.biz.executors.leads_export —— 把“待跟进”的线索导出成 CSV。"""
from __future__ import annotations

import csv

from .. import db, paths
from ..scriptkit import emit_result, load_task


def main() -> None:
    task = load_task()                         # {"task": {...}, "rule": {...}, "events": [...]}
    ops = db.open_ops()
    rows = db.rows(ops.execute("SELECT platform, nickname, profile_url, why FROM leads WHERE stage='待跟进' ORDER BY updated_at DESC"))
    ops.close()
    out = paths.ops_data() / "exports" / f"leads-{task['task']['id']}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["platform", "nickname", "profile_url", "why"])
        writer.writeheader()
        writer.writerows(rows)
    emit_result(f"导出 {len(rows)} 条待跟进线索：{out}")   # stdout 最后一行的 JSON 就是结果


if __name__ == "__main__":
    main()
```

```yaml
- id: leads-export
  name: 线索导出
  instruction: 每天早上把“待跟进”的线索导出成 CSV 放进数据目录的 exports/。
  when: {origin: cron, types: [cron.leads_export]}
  run: {executor: script, command: ["{python}", "-m", "superops.biz.executors.leads_export"], timeout: 2m}
  tell: never
```

- 脚本放在 `superops/biz/` 下，只依赖业务模块。它和宿主之间的约定（环境变量、任务文件、`/v1/*`）见 007 的“脚本契约”。运行中要回调宿主（发事件、发通知、读计数）用 `scriptkit.hub_client()`：它带着宿主给的令牌，所以脚本发的事件算可信。
- 要推进流水线，`emit_result(…, follow_up_events=[{"type": …, "subject": …, "data": {…}}])`；事件的 `subject` 决定它落在哪个线程上。
- 要人拍板，`emit_result(…, status="needs_decision", needs_decision=True, decision_card={…})`：卡片成为这张工单的问题；卡片不合规（缺标题或建议、选项不是 2–4 个、`on_expire` 不是某个选项）工单会报失败。人选中的选项里的事件由宿主带着凭据发出，通常由另一条 `requires_approval: true` 的脚本规则接着执行。`superops/biz/executors/publish.py` 的 `batch-card` 和 `run` 是一对现成的例子。
- 脚本工单只跑一次脚本，人对它说话也不会让它重跑。需要“等结果回来再接着做”的工作应当是 Agent 规则，或拆成两条脚本规则用事件接起来。

### 只留底

有些事件只要记下来、进日报，不需要任何人或 Agent 处理：

```yaml
- id: pack-rejected-note
  name: 发布包被拒
  instruction: 发布包被人拒绝只留底，进日报。
  when: {types: [content.pack.rejected]}
  run: {executor: none}
```

不写这条规则，事件也会留底，决定的原因是“没有命中已启用的规则”；写了它，原因变成“只留底”，日报的摘要会按类型统计它。`executor: none` 加 `tell: always` 是“不开工单但要立刻告诉人”，`priority: 0` 时发告警（线上核实不一致就是这样配的）。

### 对外动作的规则

执行对外动作的规则都是同一个形状：`requires_approval: true`，脚本执行器，监听一个只有卡片选项才会发出的事件类型。

```yaml
- id: reply-run
  name: 已批准的评论回复
  instruction: 人批准回复草稿后由执行器发出（不带链接和联系方式）。
  when: {types: [social.reply.approved]}
  requires_approval: true
  run: {executor: script, command: ["{python}", "-m", "superops.biz.executors.reply", "run"], timeout: 5m}
```

不要让 Agent 规则去执行对外动作：Agent 提议（卡片），人决定（选项），脚本执行（规则）。人批准的事件是明确的授权，这条规则在 `observe` 模式下也会执行；想连它一起停，暂停这条规则。测试这类规则时不能用 `superops emit` 直接发入口事件（会被过滤），要走一张真的卡片。

### 语义规则

不写 `when` 的规则由主 Session 按语义研判，用于“群里没人 @ Agent、但讨论已经定下了一件具体的事”：

```yaml
- id: script-fix-from-chat
  name: 群里定下的脚本修改
  instruction: 群里有人指出某条已有脚本的具体问题、并且已经有人给出确定的改法时，按那个改法改脚本并重新送审。不改选题和对外口径。
  run: {role: write, agent: claude, skill: ops-script-writing, tools: workspace, timeout: 30m}
  tell: needed
```

它只有在来源把没 @ 的消息也收进来时才有候选（钉钉来源 `observe: all`），而且每条这样的消息都会花一次模型调用。`instruction` 要写得能让主 Session 对着一段讨论判断“命中 / 不命中”。先在 `observe` 模式下看它的判断。

## 加一种来源

这一节实现一个叫 `wecom` 的来源：企业微信群里 @机器人 的消息成为触发，回复发回原来的回复链。它通过一个假想的命令行 `wecomq` 读写群消息；其余接口都是 superops 自己的。

假定 `wecomq` 的约定：

- `wecomq pull --after <n> --json` 打印位置 `n` 之后的消息，旧的在前：`{"me": "<机器人自己的 id>", "items": […], "next": <位置>}`。每条有 `id`、`chat`、`thread`（回复链第一条消息的 id，第一条自己没有）、`sender`（`id` 和 `bot`）、`mentions_me`、`text`、`at`。
- `wecomq reply --chat <群> --to <消息 id> --key <键> --text <正文>` 在一条消息下回复，同一个键只发一次。群已解散时以非零退出，错误输出里带 `410`。

### 1. 先把事件对应到领域

动手前定下对应关系。触发 id、线程 id 和渠道地址都会存下来，以后改会让已有的记录对不上。

| 概念 | 对企业微信群 |
| --- | --- |
| 触发 | 一条消息 |
| 线程 | 一条消息和它下面的回复：`<群>/<第一条消息的 id>` |
| 发送人 | 发送人在企业微信里的 id，名单里写的就是它 |
| 渠道地址 | 群，以及回复挂在哪条消息下 |
| 接收范围 | 一个群，在准入表里是一行 |

### 2. 写来源

放在 `superops/sources/wecom.py`：

```python
"""企业微信群来源（示例）：群里 @机器人 的消息作为触发，回复发回原来的回复链。"""
from __future__ import annotations

import json
import subprocess

from ..core.reasons import key
from .kind import SourceKind


class WecomSource(SourceKind):
    id = "wecom"
    title = "企业微信"
    interrupts = True   # 发到群里会打扰人：发件箱对它执行静默时段和每日卡片上限

    def call(self, *args: str) -> dict:
        """调 wecomq。测试里只替换这一个方法。"""
        done = subprocess.run(["wecomq", *args], capture_output=True, text=True, timeout=30)
        if done.returncode != 0:
            raise RuntimeError(done.stderr.strip()[-300:] or f"wecomq 退出码 {done.returncode}")
        return json.loads(done.stdout or "{}")

    def scope(self, meta: dict) -> dict:
        chat = meta.get("chat")
        if chat not in (self.config().get("chats") or []):
            return {"listened": False, "reason": "chat_not_listened"}
        return {"listened": True, "observed": self.config().get("observe") == "all", "row": chat}

    def tick(self) -> None:
        cursor = self.host.store.get("wecom-cursor", 0)
        page = self.call("pull", "--after", str(cursor), "--json")
        items = page.get("items") or []
        for item in items:
            trigger = self.translate(item, page.get("me", ""))
            if trigger:
                self.host.receive(trigger)
        self.host.store.set("wecom-cursor", page.get("next", cursor))   # 这一页都存下之后才挪游标
        self.report("ready", f"这一轮读到 {len(items)} 条")

    def translate(self, item: dict, me: str) -> dict | None:
        if item["sender"]["id"] == me:           # 机器人自己发的不进领域
            return None
        root = item.get("thread") or item["id"]   # 回复链的第一条消息
        thread = {"source": self.id, "id": f"{item['chat']}/{root}"}
        trigger = {
            "id": key("wecom", item["id"]),       # 由消息 id 导出：重读一遍只会是 duplicate
            "source": self.id, "threads": [thread],
            "actor": {"source": self.id, "id": item["sender"]["id"]},
            "human": not item["sender"].get("bot", False),
            "at": item["at"], "text": item["text"],
            "channel": {"source": self.id, "thread": thread["id"], "address": {"chat": item["chat"], "reply_to": root}},
            "meta": {"chat": item["chat"], "message_id": item["id"]},
        }
        if item.get("mentions_me"):
            trigger["addressed"] = "mention"
        return trigger

    def send(self, message: dict) -> str:
        address = message["channel"].get("address") or {}
        if not address.get("chat"):
            return "ignored"                      # 没有会话可回
        try:
            self.call("reply", "--chat", address["chat"], "--to", address.get("reply_to") or "",
                      "--key", message["id"], "--text", message["text"])
        except RuntimeError as error:
            if "410" in str(error):               # 群解散了：永远收不到
                return "gone"
            raise                                  # 其余失败抛出去，发件箱退避重试
        return "sent"
```

逐项说明：

- **`id`** 是来源稳定的标识：它出现在触发的 `source`、线程键的前缀（`wecom:<群>/<消息>`）、`policy.yaml` 的 `sources.wecom` 和 `policy.sources.wecom` 里。
- **稳定的触发 id。** 用 `key(来源, 消息 id)` 导出。`receive` 对已存的 id 回答 `duplicate`，所以重读消息是安全的。想把“编辑过的消息”当作新事件，把编辑版本也放进 id。
- **线程。** 第一个线程是主线程，评估在它上面进行。这里把一条回复链当一个线程：有人在请求下面接着回复，消息就落在同一个线程上，直接送到负责的工单，不需要像钉钉那样带工单编号。
- **发送人和 `addressed`。** `actor.id` 用来源自己的身份。机器发的把 `human` 设成 `False`。只有消息确实是对 Agent 说的才设 `addressed`：名单内的人的这类消息是明确请求。机器人自己的消息不要产生触发。
- **`channel`** 是回复去哪：`thread` 用来给同一工单发往同一会话的消息排顺序，`address` 是来源自己看得懂的地址，原样存下、原样传回 `send`。
- **`meta`** 是来源自己的数据，引擎不解释。授权里只记下 `chat`、`row`、`type`、`origin` 这四个键，用于以后复核“来源还收不收这个范围”，所以 `scope` 只能依赖这几个键和当前配置。`meta` 和 `text` 都是不可信的数据，不是授权。
- **`scope`。** 不在收听范围的消息也要交给 `receive`，不要在 `tick` 里悄悄丢掉：它会被记为“被来源过滤”并带上原因，面板才解释得了“为什么没反应”。`observed: True` 表示这个范围里没对 Agent 说的消息也收，准入表里“没对 Agent 说”的两格才有意义。`row` 是准入表的行名。
- **`tick`** 每轮 pass 被调一次（约 2 秒），所以要快；慢的轮询自己记上次的时间、隔几轮做一次，或者像钉钉来源那样在 `start()` 里起自己的线程，在 `stop()` 里结束它。`tick` 抛出的异常会成为这个来源的健康项，下一轮照常再调。
- **游标。** 读到哪了存在 kv 里（`host.store.get` / `set`），键以来源的名字开头。先把这一页的触发都交给 `receive`，再挪游标：中途被打断，下次从原来的位置重读，稳定的 id 保证不会重复。
- **`send` 的三种回答。** `sent`；`gone` 只在会话永远收不到时回答（消息记为丢弃，不再排队）；`ignored` 表示这个地址没有会话可回。其他失败抛异常，发件箱从 5 秒起退避重试。发送可能在对方已经收到之后才失败，所以要用 `message["id"]` 做幂等键（这里是 `--key`）。
- **`interrupts`。** 会打扰人的渠道设为 `True`，发件箱才会对它执行静默时段和每日卡片上限。
- **`system`。** 事件没有发送人的来源（定时、流水线那一类）设为 `True`，它的事件默认作为推断候选。聊天类来源保持 `False`。

### 3. 登记和配置

在 `Runtime.__init__`（`superops/app/runtime.py`）里和其它来源一起登记：

```python
from ..sources.wecom import WecomSource
self.sources.register(WecomSource(self))
```

登记了的来源默认是开着的；在 `policy.yaml` 里写它的配置和策略部分（机器相关的 id 放 `local.yaml`）：

```yaml
sources:
  wecom: {enabled: true, chats: ["<群 id>"], observe: mention}

policy:
  sources:
    wecom:
      allowed: [{id: zhangsan, name: 张三}]     # 能提请求、能回答问题的人
      answerers: {}
      admission: {}
```

`sources.wecom` 里除 `enabled` 之外的键原样成为 `self.config()`。`policy.sources.wecom` 是这个来源的策略部分，算进策略版本。想让没有会话的工单也在企业微信里找人，把 `home` 指过去：`home: {source: wecom, thread: home, address: {chat: "<群 id>"}}`。

如果这个来源也要支持“回一行就批红”，照 `superops/sources/dingtalk.py` 的做法：解析出工单编号和选项，先把这条消息作为触发 `receive`，线程用 `host.ticket_thread(工单)`（那张工单自己的线程 `ticket:T12`），再调 `host.answer_by_code(编号, 选项, by=发送人, reason=…, trigger_id=触发 id)`。权限由引擎按 `allowed` / `answerers` 判断，来源不用自己判断。有回复链的来源不需要工单编号也能跟进，但批红仍要知道“选第几个”。

### 4. 测试

用 `tests/app/worlds.py` 的 `make_world` 起一个同步模式的 Runtime，替换掉 `call`：

```python
from worlds import base_policy, make_world  # noqa: F401

from superops.sources.wecom import WecomSource


def test_wecom_request_becomes_ticket_and_reply_goes_back(make_world):
    policy = base_policy([], sources={"wecom": {"enabled": True, "chats": ["ops"], "observe": "mention"}})
    policy["policy"]["sources"]["wecom"] = {"allowed": [{"id": "zhangsan", "name": "张三"}]}
    rt = make_world(policy)
    source = WecomSource(rt)
    rt.sources.register(source)
    queue = {"me": "bot", "next": 3, "items": [
        {"id": "m1", "chat": "ops", "sender": {"id": "zhangsan"}, "mentions_me": True, "text": "帮我看看昨天那条为什么掉量", "at": "2026-11-11T09:00:00+08:00"},
        {"id": "m2", "chat": "elsewhere", "sender": {"id": "zhangsan"}, "mentions_me": True, "text": "别的群", "at": "2026-11-11T09:00:01+08:00"},
        {"id": "m3", "chat": "ops", "sender": {"id": "bot"}, "text": "机器人自己说的", "at": "2026-11-11T09:00:02+08:00"}]}
    sent = []

    def call(*args):
        if args[0] == "pull":
            return queue
        sent.append(dict(zip(args[1::2], args[2::2])))
        return {}
    source.call = call
    rt.pump()                                    # 每轮 pass 会 tick 这个来源
    [ticket] = rt.store.tickets.list()
    assert ticket["mandate"]["strength"] == "explicit" and ticket["mandate"]["basis"]["sender"] == "张三"
    assert rt.store.get("wecom-cursor") == 3
    went = {t["text"]: rt.store.triggers.disposition(t["id"]) for t in rt.store.recent_triggers(source="wecom")}
    assert went["别的群"]["filtered"]["code"] == "chat_not_listened" and "机器人自己说的" not in went
    assert [m["--chat"] for m in sent] == ["ops", "ops"] and all(m["--to"] == "m1" for m in sent)   # 确认一次、结果一次
    assert rt.health.get("source:wecom")["state"] == "ready"
```

至少覆盖：翻译（稳定的 id、线程、发送人、`human`、`addressed`，自己的消息不产生触发）；一轮读取（存一次、范围外的被过滤、游标挪到后面、名单内的人 @ 之后成为明确请求）；被打断的一轮保住游标；`send` 的 `sent` / `gone` / 抛异常；准入表的行和 `scope` 的 `row` 对得上。

最后把新模块和它的测试加进 007 的模块表和测试表：`tests/test_docs.py` 会检查。

### 清单

1. 定下触发、线程、发送人、渠道地址、接收范围的对应关系。
2. 写 `SourceKind` 的子类：`id`、`title`、`interrupts` / `system`。
3. 把事件翻译成 id 稳定的触发；丢掉自己发的。
4. `scope` 只依赖 `meta` 里的 `chat` / `row` / `type` / `origin` 和当前配置。
5. `send` 每条消息只发一次，只在会话永远收不到时回答 `gone`。
6. `tick`（或自己的线程）里先 `receive` 再挪游标，用 `report` 写健康项。
7. 在 Runtime 里登记；写 `sources.<id>` 和 `policy.sources.<id>`。
8. 测试；更新 007 的两张表。

## 加一个 Skill

Skill 是一类工作的做法：步骤、产出放哪、结构化字段。规则用 `run.skill` 引用它，执行器每一轮把它的正文拼进提示词。

```text
skills/ops-competitor-brief/
  SKILL.md        正文：用途、步骤、输入、输出、禁止事项
  schema.json     这个 Skill 要求的结构化字段
```

`SKILL.md` 开头是 `name` 和 `description` 两行元数据，正文照现有 Skill 的七节写。写的时候记住读它的是一张工单里的执行器：

- 步骤写“做什么、产出放在工单目录的哪里”，输入指到 `context/` 里的文件（由规则的 `run.context` 准备）。
- 输出一节写清这个 Skill 的结构化字段，它们放进报告的 `output`；要发哪些事件（报告的 `events`）、什么时候报告 `waiting` / `ready`、什么时候提 `question`。
- 需要等后续事件的 Skill（写稿等复核、开发等审查）要写明“收到哪种事件怎么办”：这些事件会通过订阅回到同一张工单。
- 不要在 Skill 里重复团队三段和内置提示词已经说过的规矩。
- 几个 Skill 共用的材料（如内容合规清单）放成一个文件，用 `<!-- superops:inline 相对路径 -->` 内联；只能引用 `skills/` 目录里的文件。

`schema.json` 只写这个 Skill 自己的字段：

```json
{
  "description": "竞品动态周报",
  "required": ["highlights"],
  "properties": {
    "highlights": {"type": "array", "items": {"type": "object", "required": ["competitor", "what"],
                                              "properties": {"competitor": {"type": "string"}, "what": {"type": "string"}, "link": {"type": "string"}}}}
  }
}
```

宿主在报告是 `ready` 或 `waiting` 时按它校验 `output`（第一轮必须带；之后的轮次带了才校验），不合格会让 Agent 带着错误重写一次。

可选的两步：

- **入库。** 想把结构化产出写进 `ops.db`，在 `superops/biz/ingest.py` 加一个处理函数并登记到 `HANDLERS`。它是确定性的：Agent 自己不写业务库。处理函数可以往输出里追加后续事件（监听入库后发“生成线索卡片”就是这样），这些事件算宿主发的。
- **分发。** `superops skills link` 把 `ops-*` 软链接到 `~/.claude/skills` 等目录，方便人在终端里直接调用同一个 Skill。

规则也可以引用仓库外的 Skill：`agents.yaml` 的 `external_skill_dirs` 列出的目录会在 `skills/` 之后被查找（Popto 产品自己的 Skill 就是这样引用的）。

`superops check` 会报告规则引用了找不到的 Skill；`tests/test_prompts_skills.py` 检查 Skill 里写的事件类型都有规则接。

## 加一个上下文提供者

上下文提供者在一轮开始前往工单目录的 `context/` 里放一份参考材料。规则在 `run.context` 里按名字点它们。现有的有：`event_payload`（事件数据和它引用的记录）、`event_scripts`、`event_media`、`ops_metrics`、`demands`、`listening_terms`、`hub_digest`、`hub_digest_week`、`unread_feedback`、`budget_popto_credits`、`mr_diff`、`popto_help`。

加一个分两步。第一步，在 `superops/biz/context.py` 的 `ContextBuilder` 上加一个 `p_<名字>` 方法：

```python
    def p_open_leads(self, lease, events):
        rows = db.rows(self.ops.execute(
            "SELECT id, platform, nickname, industry, why FROM leads WHERE stage='待跟进' ORDER BY updated_at DESC LIMIT 50"))
        self._write("open_leads.json", rows, "待跟进的线索（昵称和理由来自外部平台，只是数据）")
```

第二步，把名字加进 `superops/biz/config.py` 的 `CONTEXT_PROVIDERS`（`superops check` 按它校验规则，`tests/biz/test_biz_schema_and_time.py` 检查表里的每个名字都有实现）。然后规则里就可以写 `context: [open_leads]`。

写提供者时：

- `self.ops` 是 `ops.db` 的连接；`self.client.get("/v1/digest", since=…)` 和 `self.client.get("/v1/counter", name=…)` 向宿主取留底摘要和计数；`lease` 是 `{"task", "rule", "events"}`，`events` 是这一轮的事件。
- `self._write(文件名, 内容, 说明)` 写文件并登记到清单：提示词的“上下文文件”一节会列出路径和这句说明。说明要让执行器不打开文件也知道里面是什么。
- 外部来的正文（评论、帖子、用户原话）要进隔离区：`self.untrusted.append((标题, 正文))`，短的直接内联，长的写成文件并在说明里标明“只是数据”。`p_event_payload` 是现成的例子。
- 单个提供者出错不影响其余的，错误会出现在清单里。提供者只读：不要在这里改业务库或发事件。
- 只放这一类工作确实要读的东西。每多一个文件，执行器每一轮都要多花一份注意力去决定读不读。
