# 003 执行

这篇跟着一张被接下的工单走完执行、交付和恢复。它建立在 [001 领域模型](001-domain.md) 和 [002 授权](002-authorization.md) 之上。

## 工单阶段

每张未结束的工单有一个阶段，由它在等什么决定。从上往下看这张表，第一条命中的算数。

| 阶段 | 条件 | 面板和命令行里的叫法 |
| --- | --- | --- |
| 等人 / 送达 | 把输入交给执行器失败了 | 输入没送到 |
| 等人 / 回答 | 工单上有一个问题 | 等你回答 |
| 等人 / 验收 | 最近的报告是 `ready` | 等你验收 |
| 等人 / 受阻、失败 | 最近的报告是 `blocked` 或 `failed`，之后没有人的消息到过这张工单 | 受阻 · 失败 |
| 工作中 / 排队 | 还没有派工，或派工待启动 | 排队中 |
| 闲置 | 最近的报告是 `waiting`，或派工已结束 | 等后续事件 |
| 工作中 | 其余情况 | 进行中 |

“等人”的阶段表示没有人的输入工单就继续不了。人的一条新消息会把受阻或失败的工单放回给执行器。

“闲置”的工单做完了这一轮，没有悬着的问题：订阅线程上的下一条触发会让它继续。写完稿等复核结论、开完 MR 等审查意见的工单都在这个阶段。

两点和这次部署有关：不跟随任何线程的工单一报告 `ready` 就以“已完成”结束（这一轮跑的时候又送来了新输入的除外：先再跑一轮处理它们，处理完的 `ready` 才结束），所以“等你验收”只出现在跟着 MR 的开发工单上，意思是 MR 开好了，等合并或关闭。“输入没送到”这一行来自通用模型，这一版的执行器不会报告它：一轮没跑成时输入留在队列里重试。

## 两种执行器

工单命中的规则决定谁来执行（`run.executor`）。明确请求的工单没有规则，用 `policy.yaml` 里 `executor.default` 的 Agent 执行方式。

| | agent | script |
| --- | --- | --- |
| 谁在跑 | 本机的 Claude / Codex / Antigravity 命令行（WorkBuddy 走收发件箱），按 `run.role` 的降级链选 | 规则 `run.command` 写的确定性脚本，不调用大模型 |
| 目录 | `work/<工单短名>/` | `runs/<工单短名>/` |
| 轮数 | 每次有新的输入跑一轮，可以很多轮 | 只在第一轮跑一次；之后只对人的回答作出反应 |
| 输出 | 一份报告（见下） | stdout 最后一行的 JSON：`status`、`summary`、`decision_card`、`follow_up_events`、`message_markdown` |
| 用在 | 写作、分拣、研判、审查、开发、浏览器操作 | 采集、登记、发布 / 回复 / 合并 / 清理的执行、生成发布卡片和线索卡片 |

`executor: none` 的规则不开工单，只留底。

工单短名是 `t<编号>-<名字>`：规则起的工单用规则 id（`t12-script-writing`），明确请求的工单用主 Session 起的英文短名。

## 一轮怎么跑

工单有等待的输入、且有空的执行位时，执行器跑一轮：

1. **取输入。** 这一轮处理工单上所有还没送达的输入。第一轮还包括起点的那批触发。
2. **准备上下文。** 按规则的 `run.context` 往 `context/` 里放参考材料（事件数据、近 14 天指标、需求池、留底摘要、帮助文档、MR 的 diff 等）；外部内容另外放进隔离区。规则配了 Skill 的读出 `SKILL.md`。从 `decisions.md` / `not-doing.md` 里按工单标题、规则名和对象挑出相关的决定。
3. **准备环境。** `tools: browser` 的先开好专用浏览器；`run.worktree` 的先建好代码 worktree（见下）；视频类的工作（`role: video`）落到不能直接看视频的 Agent 时，先把视频抽帧拼成宫格并转写。
4. **选 Agent 并运行。** 按降级链找第一个可用且有并发空位的 Agent；审查类的角色（`role: review`）先把产出方排除。提示词按这个 Agent、这一轮现拼；Agent 的输出必须是一份符合 Schema 的报告。
5. **校验。** 报告不符合 Schema、有问题却报 `ready`、卡片不合规、Skill 要求的字段缺了，都算不合格：带着错误让同一个 Agent 重试一次，还不行换下一个。
6. **收尾。** 关浏览器；把工单目录里的文本打码；记下这一轮读了哪些输入；存下 Agent 的会话 ID；开发工单推送分支并开 MR。
7. **把报告落到领域里**（下一节）。

脚本工单的一轮：把任务写成 `runs/<工单短名>/task.json`（工单、规则、起点事件），带着环境变量拉起脚本，读它 stdout 最后一行的 JSON。脚本的 `follow_up_events` 作为可信事件发出；有 `decision_card` 就成为工单的问题，报告 `waiting`，卡片不合规则报告 `failed`（卡片发不出去不能当成做完了）；`status: failed` 报告 `failed`；否则报告 `ready`，工单结束。人回答了卡片之后，脚本工单的第二轮只做一件事：报告 `ready`（“某某选了「…」”）。

脚本做的是确定性的、常常是对外的动作，所以绝不因为一句话重跑：已结束的脚本工单被人的消息恢复时，宿主把这条消息交还去评估（由主 Session 决定它是不是一件新的工作），工单沿用原来的报告再次结束，脚本不执行。要重跑脚本：失败的工单用“重试”；已完成的重新发一次它的触发（定时类用 `superops fire <cron.类型>`）。

## 模型和思考强度

同一个 Agent 干的活轻重差得很远：主 Session 研判一天几百次，要快；排选题、写 PRD 一周几次，要想得深。所以模型和思考强度按“哪个 Agent × 干哪种活（角色）”配，写在 `agents.yaml` 的 `agents.<名>.models`：

```yaml
claude:
  default_model: claude-opus-5-5
  default_effort: medium
  models:
    route:    {model: claude-sonnet-5-5, effort: medium}   # 主 Session 研判（policy.yaml 的 eval.role）
    decide:   {model: claude-fable-5-2, effort: high}      # 排选题、周报、竞品评审
    classify: {model: claude-sonnet-5-5, effort: low}      # 评论分拣、线索、需求归并
    dev:      {model: claude-fable-5-2, effort: high}      # PRD、开发、按审查意见改
```

一轮实际用哪个，按顺序取第一个有的：

| | 依次取 |
| --- | --- |
| 模型 | 规则的 `run.model` → `models.<角色>.model` → `default_model` → 命令行自己的默认（Codex 是 `~/.codex/config.toml`） |
| 强度 | 规则的 `run.effort` → `models.<角色>.effort` → `default_effort` |

- 规则的 `run.effort` 是某一类任务特意定的（写脚本、竞品调研 `high`，合规复核 `medium`），优先于角色的默认。
- 规则的 `run.model` 写成字符串时只给规则指定的那个 Agent（`run.agent`）；降级到别的 Agent 时不带过去，免得把 Claude 的模型名传给 Codex。要给每个 Agent 各指定一个，写成 `{claude: opus, codex: gpt-6.1-sol}`。
- 强度档位 `low` / `medium` / `high` / `xhigh` / `max`，Claude（`--effort`）和 Codex（`model_reasoning_effort`）都原样收。Claude 的模型写全名（`claude-fable-5-2` / `claude-opus-5-5` / `claude-sonnet-5-5` / `claude-haiku-5-5`），版本固定，面板和运行记录里看得到具体版本；也能写别名 `fable` / `opus` / `sonnet`，跟着命令行自动换最新版。
- 每次运行实际用的模型和强度记在运行记录里（`superops ticket T12` 的运行、面板工单详情和“一条指令的旅程”都能看到）；面板运转图的 Agent 卡片上按“模型 · 强度：哪些角色”列出配置。
- `superops check` 校验：`models` 里的角色要在 `fallback_chains` 里、强度要是上面五档之一、`eval.role` 要有降级链；脚本和收件箱类的 Agent 不调用模型，配了会提醒。

现在的配置（理由写在 `agents.yaml` 的注释里）：

| 角色 | Claude | Codex |
| --- | --- | --- |
| route 主 Session 研判 | claude-sonnet-5-5 · medium | gpt-6.1-sol · medium |
| decide 拍方向 | claude-fable-5-2 · high | gpt-6-astra · high |
| write 撰写 | claude-opus-5-5 · medium（写脚本、竞品调研在规则里定 high） | gpt-6.1-sol · medium |
| classify 分拣 | claude-sonnet-5-5 · low | gpt-6-luna · low |
| dev 开发 | claude-fable-5-2 · high | gpt-6.1-terra · high |
| review 复核审查 | claude-opus-5-5 · high（兜底） | gpt-6-astra · high（合规复核在规则里定 medium） |
| ops 排障 | claude-opus-5-5 · high（兜底） | gpt-6-astra · high |
| browser 浏览器 | claude-sonnet-5-5 · medium（兜底） | gpt-6.1-sol · medium |
| video / image 看视频、看图 | claude-opus-5-5 · medium（兜底） | gpt-6.1-sol · medium |

Antigravity 不可用期间不配；WorkBuddy 走收件箱，模型由接活的应用自己定；脚本不调用模型。

## 报告怎么落地

Agent 每一轮的输出是一个 JSON 对象。宿主按下面的顺序把它落下来；任何一步被领域的规则拒绝，只丢掉那一项，报告本身不丢。

| 字段 | 落到哪 |
| --- | --- |
| `ack` | 对范围内的人的请求确认：发到那条消息所在的会话。每条触发只会有一条确认 |
| `handback` | 把超出这张工单的人的消息交还，带原因；没有别的工单决定它时进入评估 |
| `unrelated` | 取消工单对一个无关线程的订阅，并交还那里由它决定的消息 |
| `follow` | 为了解上下文关注一个线程（线程键，如 `pipeline:content:c_1`） |
| `output` | Skill 要求的结构化字段。按 Skill 的 `schema.json` 校验后，由入库程序写进 `ops.db`（选题、脚本、评论分类、需求、线索等）。入库程序是确定性的，它自己追加的后续事件（如监听入库后的“生成线索卡片”）算宿主发的 |
| `events` | 逐条成为流水线来源的触发，宿主在事件数据里补上产出它的工单（`parent_ticket`）、工单目录名（`parent_task`）和 Agent（`producer`）。以 `_` 开头的键全部丢掉。需要人批准的类型在这里被拦下 |
| `dogfood_friction` / `ops_feedback` | 各成为一条 `ops.dogfood.friction` / `ops.system.feedback` 事件：前者由规则归并进需求池，后者只留底进日报 |
| `credits_used` | 累加到当日的 Popto 积分计数 |
| `state`、`summary` | 记为这次派工的报告。`blocked` / `failed` 之后系统事件不再送达，直到有人的消息 |
| `question` | 成为工单的问题，渲染成卡片发出；`null` 清掉原来的问题 |
| `answer` | 回答人的一条提问：发到那条消息所在的会话。答复已经把结果告诉了提问的人，同一轮的 `ready` 就不再另发一遍结果 |
| `message` | 给人看的正文，按下面的规则决定发不发 |
| `mr_title`、`mr_description` | 开发工单：宿主开 MR 时用。推送后 MR 链接和分支会写进 `output` 一起入库 |
| `artifacts` | 产出文件的路径，随报告留在工单目录的 `output.json` 里 |

**结果发不发给人**：

- `ready`（或带了 `message` 的 `dropped`）：授权强度是明确的工单总会回话；推断的工单只有规则是 `tell: always` 才发。发到工单开始的会话（没有会话的是 home 渠道），正文是 `message`，没有就用 `summary`。
- `blocked` / `failed`：强度明确的工单，或优先级 P0 / P1 的规则，才通知人；其余进日报。
- “强度明确”包括两种工单：人直接提的请求，以及人批准的事件开出的工单。只有前一种“有人在等回话”，总会回结果；后一种照规则的 `tell` 办：写稿规则写着 `tell: never`，它的工单做完就不出声。
- `waiting`：不出声。要人行动的事只能通过 `question` 到人那里。

## 并发与执行位

- 一张工单同一时间只跑一轮；上一轮没结束时新的输入等着。
- 同时运行的轮数不超过 `executor.max_workers`（默认 4）。带着人的消息或人的回答的那一轮可以多占一个位子，所以新工作挡不住人继续已有的工单。
- 每个 Agent 有自己的并发上限（`agents.yaml` 的 `concurrency`：Claude 3、Codex 2、Antigravity 2）。主力只是忙（位子满了）时这一轮先排队，等满 `overflow_after`（10 分钟）才顺延给降级链上的下一个，每多等这么久最多再往后一位：不会因为一时忙就掉到最弱的 Agent。熔断、暂停或本机没装的不等，直接顺延。
- 专用浏览器同一时间只能被一个 Chrome 打开，`tools: browser` 的轮次串行。
- 规则写了 `after` 的工单，启动前等那些规则的工单跑完（日报等采集），最多等 `after_max`。

## 目录与会话续接

Agent 工单的目录 `~/popto-superops-data/work/<工单短名>/` 从第一轮用到最后一轮：

| 文件 | 内容 |
| --- | --- |
| `turn-NNN-<agent>.md` | 每一轮（每一次尝试）发给 Agent 的完整提示词 |
| `turn-NNN-<agent>.stdout` / `.stderr` | 那一次的原始输出 |
| `context/` | 这一轮的参考材料 |
| `output.json` | 最近一轮通过校验的报告 |
| 其余 | Agent 的产出（脚本、发布包、PRD 等），路径相对工单目录 |

主 Session 的每次研判在 `work/_router/<日期>/<评估 id>/` 下留同样的文件。

工具权限按规则的 `tools` 档位给：`readonly` 只读工单目录；`workspace` 可以在工单目录里写文件、跑 python / uv / ffmpeg / node；`browser` 另外能用 `ops-browser` 操作专用浏览器（Claude 还能用网页搜索和网页抓取）；`dev` 另外能用 git 和构建工具，但不能强推、推主干或合并。处理评论等不可信输入的规则一律 `readonly`。Codex 的 `workspace` / `dev` 档默认断网（沙箱 workspace-write）；要调外部接口的规则（如行业爆款拆解调 Popto 开放 API）在 `run` 里写 `network: true` 放开，`readonly` 档不放开。只读档的提示词告诉 Agent 怎么读文件：Claude 用 Read / Grep（Bash 被禁），Codex 没有读文件工具，用 `cat`、`rg` 这类只读命令（只读沙箱允许读、拦写和联网）；2026-11-12 以前提示词写“不能运行任何命令”，Codex 因此什么都读不了。审查类规则在 `run` 里写 `conclude_with`（合规复核：通过 / 打回；视频质检：通过 / 返工；MR 审查：review.done）：工单没发过其中任何一个事件就报完成或无需处理的，宿主改成受阻并通知人；带问题卡片的不拦。Agent 的环境变量里只有工单目录、数据目录和仓库根，没有管理 API 的地址和令牌：它要发的事件写在报告里，由宿主来发。

**会话续接**：每一轮结束，宿主把 Agent 返回的会话 ID 按“工单 × Agent”存下来。`agents.yaml` 里开了 `resume` 的 Agent（Claude、Codex）下一轮带着它启动（Codex 续接轮直调本体，沙箱放在顶层参数里），接着同一段上下文，提示词也换成精简版。换了 Agent（降级）或 Agent 不支持续接时没有会话可接，提示词给完整版，并附“这张工单到目前为止”：之前各轮的报告摘要和人作过的决定。

## 开发任务：worktree 与 MR

规则写了 `run.worktree: true`（需求 → PRD → 开发、作者修复）的工单，每一轮开始前宿主准备好代码环境：

- 仓库从事件数据的 `project` 找，没有就用 `ci.default_project` 或唯一的 `ci.projects`；本机路径在 `local.yaml` 的 `ci.repos`。
- 新需求：从 `origin/<基线分支>` 建分支 `agent/<工单短名>`，worktree 放在仓库同级的 `<repo>-agent-<工单短名>`。修复已有 MR：复用 MR 的源分支。
- 主仓库装好的 `node_modules` 软链进来（仅当它被 git 忽略）。

Agent 在 worktree 里改代码、跑检查、`git commit`，不 push。这一轮结束后（报告不是 `failed`）宿主来做：有新提交就推送，没有打开的 MR 就新建，描述末尾补一行 `producer: <Agent 名>`；只写了 PRD、没有提交的新 worktree 直接删掉。

MR 开出来之后：

1. 工单**跟随** MR 的线程 `pipeline:mr:<项目>!<编号>`，并标记这是它自己开的。
2. 宿主发出 `gitlab.mr.opened`（之后的推送是 `gitlab.mr.updated`）。它不回送给开发工单自己，而是命中 MR 审查规则，开出一张审查工单，执行的 Agent 不是写代码的那个。
3. 审查结论是这个 MR 线程上的事件，通过订阅回到开发工单：它在同一个 worktree、同一个分支上接着改。落在这个线程上的其他事件（例如流水线失败）同样会送到它。
4. MR 合并或关闭的事件到达时，流水线来源告诉引擎“这个线程关了”，开发工单随之以“已合并”或“已关闭”结束。

跟随线程的工单报告 `ready` 不会结束，它等线程关闭。线程关闭时如果工单还有没做完的事（恢复后的工作还没报告 `ready`、刚接下一条人的跟进），先不结束，等它做完。

哪些事件类型算“关闭”在流水线来源里：默认 `gitlab.mr.merged` → 已合并，`gitlab.mr.closed`、`content.closed` → 已关闭；`sources.pipeline.closes` 可以加。

## 工单怎么结束

| 怎么结束 | 结果 |
| --- | --- |
| 报告 `ready`，不跟随任何线程，也没有这一轮期间新送来的输入 | 已完成 |
| 报告 `dropped`：查下来没有需要人处理的事 | 无需处理。不发消息，除非报告带了 `message` 且工单的强度是明确或规则是 `tell: always` |
| 它跟随的线程关了（MR 合并 / 关闭） | 已合并 / 已关闭 |
| 人停掉：`superops stop T12`、面板上的停止，或名单内的人在群里明确要求、主 Session 决定停止 | 被人停止。授权撤回，之后什么都不收 |
| 闲置超期：一直在“等后续事件”，超过规则的 `idle_ttl`（默认 7 天）没有任何更新 | 无需处理，原因写“闲置超期” |

除了被人停掉的，结束的工单都能被人恢复：人在它的线程上发消息（钉钉里带着编号回复，落在它自己的线程 `ticket:T12` 上）会恢复它。结束的工单保留它的订阅，只退订共用的定时线程 `schedule:<类型>`。脚本工单被恢复时不重跑脚本，见上。

问题到期不直接结束工单：宿主替人选了安全选项，执行器收到这个“决定”后自己收尾。

## 失败与恢复

### Agent 不可用

宿主按 Agent 的错误输出归类，决定熔断多久：

| 错误 | 处理 |
| --- | --- |
| 额度 | 熔断 60 分钟，或错误信息里写的恢复时间；这一轮立刻走下一个 Agent |
| 认证 | 熔断 6 小时，健康板报错；这一轮走下一个 |
| 网络 | 冷却 5 分钟，不算这个 Agent 试过 |
| 超时 | 换下一个；同一 Agent 连续 3 次超时熔断 30 分钟。浏览器档位的轮次（Antigravity、Codex）连续 10 分钟没有任何动作也算超时、提前结束：每 15 秒看一次标准输出 / 错误和运行目录（含浏览器页面快照）有没有新变化（`agent_browser.idle_timeout`，0 关掉）。Claude 不启用：`-p` 模式最后才输出，查网页也不写文件，会被误杀 |
| 输出不合格 | 带着校验错误同一 Agent 重试 1 次，还不行换下一个 |
| 其他错误 | 换下一个 |
| 只是忙（并发满） | 不算错误：这一轮排队，等满 `overflow_after` 才顺延 |

冷却结束后放一轮进去试，成功就恢复。降级链上还有没试过的 Agent 只是在忙、熔断中或被暂停时，这一轮排队等（30 秒后再试），输入留在队列里，不记失败。这一轮已经试过并失败的 Agent 记在库里，排队期间不会被重新拉起。链上的 Agent 都试过且都没成，工单报告 `failed`。

`route`、`decide`、`dev`、`review` 的降级链只有 Claude 和 Codex：它们都不可用时工单等着，不交给更弱的 Agent。一个 Agent 持续不可用超过 `alert_after_unavailable`（2 小时）发一条 P0 告警。`superops agents` 看状态，`superops agents reset <名>` 手动解除熔断。

### 受阻、失败与重试

工单报告 `blocked` 或 `failed`（包括执行器内部出错）之后，系统事件记在工单上但不送达，也不唤醒它。解除的办法有三个：人在它的线程上发一条消息；重试；它后来的一份不是 `blocked` / `failed` 的报告。

重试（`superops retry T12` 或面板上的按钮）只对卡在受阻、失败的未结束工单可用：结束当前的派工，开一次新的，目录、分支和 MR 都不变。

### 宿主停止与重启

宿主停止时取消正在跑的轮，Agent 和脚本的子进程整组结束，不留下孤儿进程继续写工单目录。重启时这些还在执行中的派工按“被打断”处理，最老的工单先来：

- 最近的报告是 `waiting` / `ready` / `blocked` / `failed`、之后没有新的输入送达的，保持原样。
- 其余的结束（原因：superops 重启），开一次新的派工，先收到一条恢复说明：先检查工作目录、已产出的文件和外部状态（已发的事件、MR），不要重复已经完成的对外操作。

待启动的派工重新排队。工单目录、会话 ID、订阅、等待的输入、没发出的消息都在库里，重启不丢。

### 先落库，再执行

宿主在一个决定之后要做的外部动作（研判一个候选、启动一次派工、跑一轮、发一条消息）都先作为一条副作用存进库里，事务提交之后才执行。失败按指数退避重试：

| 副作用 | 同时最多 | 重试 |
| --- | --- | --- |
| 研判 | 4 | 2 秒起，翻倍到 60 秒 |
| 启动派工 | `max_workers` | 5 秒起，翻倍到 300 秒 |
| 跑一轮 | `max_workers` + 1 | 2 秒起，翻倍到 60 秒；没有空位或 Agent 都在忙时稍后再试，不计失败 |
| 发消息 | 1 | 5 秒起，翻倍到 300 秒 |

同一类副作用里有一项连续失败 3 次，才作为系统问题出现在面板和 `superops status` 上；一次偶发的网络错误不算问题。

### 渠道失败

发往渠道的消息先存后发。同一张工单发往同一个会话的消息按排队顺序发：确认在结果之前。前一条还没发出去时后一条等着，直到前一条连续失败 3 次。来源关着时消息等着；来源说这个会话永远收不到了，消息记为丢弃并留下原因，不会再排队。

发往钉钉的消息还要过两道闸：静默时段（`quiet_hours`，默认 22:30–08:00）里只发 P0，其余等到结束；问题卡片每天最多 `max_cards_per_day`（10）张，超出的顺延。

### 暂停处理

`superops pause`（不带参数）暂停全部处理：不开始新的评估，不启动派工，不跑新的一轮，不发消息，问题不到期，闲置的工单不清。来源照常记录触发，正在跑的那一轮不打断。人的消息到了已结束的工单会等着，恢复处理后的第一轮再唤醒它（授权届时仍要过复核）。进行中的评估在暂停期间不超时，期限顺延暂停的时长。`superops resume` 恢复。

也可以只暂停一部分：`superops pause <规则 id 或名字>` 暂停一条规则（命中它的事件记为忽略，原因是规则已暂停）；`superops pause agent:<名>` 让降级链跳过一个 Agent；`superops pause origin:<事件来源>` 让流水线来源不再收这个来源的事件（记为过滤）。这三种不改变策略版本。

## 预算

| 预算 | 在哪 | 怎么执行 |
| --- | --- | --- |
| Popto 积分 | `agents.yaml` 的 `budgets.popto_credits`（单日 200） | 规则写了 `credits_per_run` 的，接单前检查“今日已用 + 这次预计”会不会超；超了记为忽略（超预算）。实际用量来自报告的 `credits_used` |
| TikHub 请求 | `budgets.tikhub_requests` | 指标采集脚本自己检查 |
| Claude 周用量 | `usage_policy` | `superops agents usage claude 0.75` 登记后，P2 的工作把 Claude 排到链尾，P3 不用 Claude |
| 问题卡片 | `max_cards_per_day` | 发件箱顺延 |
| 会打扰人的规则 | `max_tell_always_rules`（4） | `superops check` 拒绝超出的策略 |
