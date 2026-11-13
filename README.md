# superops · 产品运营研发多 Agent 系统

superops 是 Popto 产品运营研发多 Agent 系统（本机运行）：定时、业务事件和钉钉群里的话进来，系统判断该不该开工，开工的事在一张工单里由本机的 Claude / Codex / Antigravity（或确定性脚本）做完，只有要人拍板的事才变成一张带选项的问题卡片发给人。它是 Python 包 `superops`，一个进程独立运行，不依赖别的宿主。

它按一条原则设计：**尽量少占人的注意力，尽量多给价值。** 放到运营场景里就是：采集、日报、写稿、复核、发布包这些事自己往下走，不在群里汇报进展；发布、回复评论、合并代码、删东西这些对外动作，Agent 只能写成卡片上的选项，人在群里回一行 `T12 1` 才会执行；没人直接要的事，只有团队在 `policy.yaml` 里写了规则才做，而且新装的系统默认只记录“本来会接单”，不动手。[docs/000-philosophy.md](docs/000-philosophy.md) 讲这条原则和它对操作者的要求。

架构和领域模型来自 DeepSeek 团队的研究原型 dsh-superagent（MIT 许可，一个跑在 DeepSeek Harness 里、面向 GitHub 仓库的编码 Agent）。`superops/core/` 是它 `src/core` 的逐行移植；来源、策略、执行器和业务模块是本项目的。与原型的差异见 [docs/007-architecture.md](docs/007-architecture.md) 末尾。

## 和旧 ops 的关系

同级目录的 `ops/`（包 `popto_ops`）是上一版：事件中台 hub + 接单程序 opsd + `rules.json` 静态路由 + 一次性任务。superops 把同一套业务按新架构重做，业务模块（采集器、发布 / 回复 / 线索 / 清理执行器、CI、专用浏览器、打码、数据保留）原样带到 `superops/biz/`。两套的端口和数据目录不同，可以同时跑，也可以同听一个钉钉群（旧 ops 只认 `C12`，superops 只认 `T12`）；但对外动作只能有一套在做，切换步骤见 [docs/006-deployment.md](docs/006-deployment.md)。

| 旧 ops | superops | 变了什么 |
| --- | --- | --- |
| 事件（`hub.db`） | 触发 | 每条触发属于一个线程（一个业务对象一个线程），只存一次 |
| `rules.json`：第一条命中的规则生成任务 | 策略规则（`policy.yaml`）+ 评估 | 规则是“可以主动做什么”的授权依据；先过准入，再研判；人直接提的请求不需要规则 |
| 任务：一个事件跑一次就结束 | 工单 | 带授权和范围；能接着收同一线程上后来的事件和人的消息；结束后人在线程上说话还能恢复 |
| 每个任务拉起一次性的 Agent | 工单的会话 | 一张工单可以跑很多轮；Claude（`--resume`）和 Codex（`exec resume`）接着自己上一轮的会话，换了 Agent 就靠提示词里的“这张工单到目前为止”（见 [009 会话](docs/009-sessions.md)） |
| 卡片 C12（独立对象） | 工单的问题 T12 | 卡片是工单“在等人回答”的那个问题，编号就是工单号 |
| `follow_up_events` 回到中台再路由 | 报告里的 `events` → 流水线来源的触发 + 订阅 | 同一对象的后续事件直接送到负责它的工单：复核结论回到写稿工单，审查意见回到开发工单 |
| 同组串行、防抖 | 线程上的评估串行；来源给触发写 `notBefore` | 评估自己从不等待 |
| hub、opsd、钉钉监听三个进程，opsd 长轮询领任务并续租 | 一个进程 `superops run` | 没有任务队列和租约；对外动作先落库再执行 |
| 通知出口 notifier | 发件箱 | 仍是唯一找人的出口：静默时段、每日卡片上限 |
| `approvers` / `card_approvers` / `card_chats` | `policy.sources.dingtalk` 的 `allowed` / `answerers`，`sources.dingtalk.rule_chats` | 限定审批人和分群都按规则 id 配，不再按卡片类型 |
| `opsctl dashboard` 生成静态看板 | 面板（和管理 API 同一个地址，实时） | |
| `opsctl task <id>` | `superops ticket T12` | |
| `127.0.0.1:8787`，`~/popto-ops-data/`，`hub.db` | `127.0.0.1:8797`，`~/popto-superops-data/`，`superops.sqlite` | 业务库 `ops.db` 结构相同，各用各的文件 |

没有带过来、或做法变了的：

- **接单模式**是新的：`observe`（默认）下没人要求的主动工作只记录不执行，包括采集和日报。
- **积压的卡片合并成一条“待批清单”**没有带过来：超过每日上限的卡片顺延到次日，逐张发。
- **改稿、修复不再是新任务**：`content.script.revise` 这类“返工事件”不用了，复核打回通过订阅回到原来的写稿工单。
- **暂停的写法**：`暂停 <规则 id 或名字>` 暂停一条规则，`暂停 agent:<名>` 暂停一个 Agent，其余写法（如 `暂停 douyin`）按事件来源算：流水线来源不再收这个来源的事件。它不会拦下已经批准、正要执行的发布。
- **Agent 的输出换了形状**：不再是 `status` / `decision_card` / `follow_up_events`，而是一份工单报告（`state` / `question` / `events` / `output` 等，见 [AGENTS.md](AGENTS.md)）。确定性脚本的输出和 `/v1/*` 接口保持旧的约定，所以业务脚本没有改。
- `hub.db` 里的事件、任务、卡片不迁移；旧的 `rules.json` 留在 `config/ops-rules.reference.json` 仅供对照。

## 目录

| 路径 | 内容 |
| --- | --- |
| `policy.yaml` | 策略：接单模式、执行器默认值、home 渠道、各来源的开关 / 名单 / 准入表，以及 47 条规则 |
| `agents.yaml` | 机器层面的设置：管理 API 地址、Agent 启动方式和并发、降级链、熔断、预算、静默时段、数据保留期、自动发布模式 |
| `schedules.yaml` | 定时来源的时间表（13 项） |
| `decisions.md` / `not-doing.md` | 定的事 / 定了不做的事（只追加） |
| `prompts/` | 内置提示词（`*.md`）、团队段落（`team/*.md`）、钉钉自动回复的规矩（`auto-reply.md`） |
| `skills/ops-*/` | 15 个流水线 Skill：`SKILL.md` + `schema.json` |
| `listening/keywords.yaml` | 社媒监听的意图词 |
| `launchd/template.plist` | 常驻进程的 plist 模板 |
| `superops/core/` | 领域引擎：触发、线程、评估、工单、派工、订阅的规则，不做 I/O |
| `superops/app/` | 应用层：SQLite、副作用队列、发件箱、设置与策略、研判、组合根、提示词、管理 API |
| `superops/sources/` | 来源：定时、流水线事件、终端、钉钉（含单聊和 @我 的监听，交给自动回复） |
| `superops/executor/` | 执行器：Agent 池（降级、熔断、输出校验）、工单的一轮怎么跑、各 Agent 的调用方式 |
| `superops/biz/` | 业务模块：采集器、执行器、CI、专用浏览器、打码、数据保留、`ops.db`、入库 |
| `superops/panel/index.html` | 面板（单页，和管理 API 同址） |
| `superops/cli.py` | `superops` 的全部子命令 |
| `docs/` | 编号文档 000–008 和 [整体流程](docs/整体流程.md) |
| `tests/` | `uv run pytest -q` |

数据目录 `~/popto-superops-data/`（不进 git，700 权限）：`superops.sqlite`（触发、线程、决定、工单、消息、运行记录）、`ops.db`（业务数据）、`local.yaml`（本机配置）、`auto-reply-context.md`（钉钉自动回复用的个人近况，自己改）、`work/<工单短名>/`（Agent 工单的目录：每轮的提示词、日志、`context/`、产出）、`runs/<工单短名>/`（脚本工单的 `task.json` 和日志）、`notify/console.log`（本机收件箱）、`logs/`、`browser-profiles/`。

## 上手

```bash
uv sync                      # 装依赖需要代理时先 export https_proxy=… http_proxy=…
uv run superops init         # 建数据目录和两个库，生成 ~/popto-superops-data/local.yaml
uv run superops doctor       # 检查 Agent CLI、dws、ffmpeg、钥匙串、策略、宿主是否在跑
uv run pytest -q
uv run superops run          # 前台启动：来源、评估、执行器、发件箱、管理 API 和面板（127.0.0.1:8797）
```

新装的系统是 `observe` 模式：定时和业务事件照常进来、照常评估，但命中规则的只记一条“本来会接单”，不开工单、不拉起 Agent、不跑脚本。看一阵记录：

```bash
uv run superops decisions --kind would_accept   # 本来会接哪些
uv run superops status                          # 等你处理的、系统问题、过去 24 小时的流量
uv run superops mode auto                       # 看着没问题，再放开
```

名单内的人直接提的请求在任何模式下都有效，可以先用它试一条完整的链路（没配钉钉时，确认和结果落在本机收件箱）：

```bash
uv run superops ask "帮我看看昨天那条抖音为什么掉量"
uv run superops tickets --filter all
uv run superops inbox
```

### 正式启用前要配的

1. **钉钉**：在 `local.yaml` 里打开钉钉来源、填运营群，并把 home 渠道指到这个群（`superops init` 生成的模板里有注释好的样子）：

   ```yaml
   home: {source: dingtalk, thread: home, address: {chat: "<运营群 openConversationId>"}}
   sources:
     dingtalk: {enabled: true, chats: ["<运营群 openConversationId>"], observe: mention}
   policy:
     sources:
       dingtalk:
         allowed: [{id: "<openDingTalkId>", name: "审批人"}, {id: "<userId>", name: "审批人"}]
         answerers: {lead-card: ["<只能批线索卡片的人的 ID>"]}
   ```

   群 ID 用 `dws chat +chat-search --query 运营` 查。`allowed` 里的人能在群里提请求、批所有问题、下指令；群消息事件带 `openDingTalkId`，卡片回调带 `userId`，两个都填，不认昵称。`answerers` 按规则 id 限定只能批某一类问题的人。想把某条规则的问题卡片发到另一个群，写 `sources.dingtalk.rule_chats: {lead-card: "<群 ID>"}`，那个群也会被监听。改完重启宿主。
2. **密钥和登录**：见下面的“要提前登录的和要提前放好的密钥”。
3. **CI**：`local.yaml` 的 `ci.projects` 填要接入的仓库（如 `popto/frontend_popto`），`ci.repos` 填各项目在本机的路径；`ci.auto_merge` 保持 `false`（合并都发卡片），跑稳后再打开；`ci.verify` 给每个项目配线上版本地址（如 `https://…/version.json`，内容里要有提交号），合并后自动核实上线，没配的合并后会请人确认上线（见 006 的“开发与合并”）。
4. **常驻**：`uv run superops launchd install` 生成 `~/Library/LaunchAgents/cn.popto.superops.plist`，确认无误后 `uv run superops launchd install --load`。
5. **Agent 专用浏览器**：`uv run superops browse install`（把 playwright-cli 装到数据目录 `tools/`，要走代理时加 `--proxy <npm 代理>`），再 `uv run superops browse login` 打开有界面的 Chrome，把要用的站点逐个登录后回车（`uv run superops browse sites` 看清单，也可以只登几个：`superops browse login douyin doubao`）。登录态存在 `~/popto-superops-data/browser-profiles/agent-browse`，和发布用的各平台目录分开；建议用专门的小号。`uv run superops browse check` 检查。
6. **自动发布的登录**（要用才配）：`uv run superops publish login douyin`，`uv run superops publish check` 检查；各平台的模式在 `agents.yaml` 的 `publishers`（默认 `dry_run`；`draft` 填好存草稿不发布，`live` 直接发布）。发布时间按发布包里的 `scheduled_at`：批准时离它还来得及就用平台的“定时发布”，来不及就立即发布（细节见 [docs/006-deployment.md](docs/006-deployment.md) 的“自动发布”）。
7. **Skill 分发**（可选）：`uv run superops skills link` 把 `ops-*` 软链接到 `~/.claude/skills` 和仓库内的 `.claude/skills`、`.agents/skills`。

每一步的细节、从旧 ops 切换的步骤和排障表在 [docs/006-deployment.md](docs/006-deployment.md)。

### 要提前登录的和要提前放好的密钥

发布和采集要用登录态，采集和 CI 要用密钥。这些都要在放开自动（`superops mode auto`）之前准备好，否则到点了才会发现：晚上的发布转了人工、评论没采到。

| 要准备的 | 谁用得到 | 怎么准备 | 放在哪 |
| --- | --- | --- | --- |
| 抖音 / 视频号 / 小红书创作者后台登录 | 执行已批准的发布（自动发布、试运行、存草稿）；到点的指标快照和手动采集（读创作者后台，复用同一份登录态）；线索跟进并发评论（抖音） | 面板“内容 → 账号”扫码，或 `superops publish login douyin`（`wechat_channels`、`xiaohongshu` 同理）：打开有界面的 Chrome，人扫码。同一平台可以加多个号（面板“添加账号”，账号键 `douyin@2`…），不指定账号的发布、评论引流、登录检查用这个平台的默认账号 | `~/popto-superops-data/browser-profiles/<账号键>/`（主号的账号键就是平台名） |
| Agent 专用浏览器登录 | 社媒监听的浏览器补位（知乎、B站、搜狗微信）、GEO 检查 | `superops browse install`，再 `superops browse login`（建议用专门的小号） | `~/popto-superops-data/browser-profiles/agent-browse/` |
| TikHub 密钥 | 社媒监听（接口：搜作品、按人群词找人、补粉丝数）；评论采集；抖音指标读不到后台时的兜底 | `security add-generic-password -s popto-ops/tikhub -a popto-ops -w <key>` | 钥匙串 |
| GitLab 令牌（机器人账号，api 权限） | 接入了仓库（`ci.projects` 非空）时的 CI 轮询、MR 审查与写回、合并闸门、合并、开发工单的推送和开 MR | `security add-generic-password -s popto-ops/gitlab -a popto-ops -w <token>` | 钥匙串 |
| Agent 命令行已登录（Claude / Codex / Antigravity） | 所有 Agent 执行的规则 | 各自的命令行登录一次；`superops doctor` 看包装脚本在不在 | 各 CLI 自己的目录 |
| `dws` 已登录 | 钉钉来源（收群消息、发卡片） | `dws auth login` | dws 自己的配置 |

登录态和密钥都**不跟着数据目录以外的东西走**：新建数据目录（包括从旧 ops 切过来）后，三个创作者后台和专用浏览器都要重新登录一次；钥匙串的服务名沿用 `popto-ops/<名>`，旧 ops 配过的不用重配。扫码登录只能由人来做，Agent 不会去登录任何站点。

**服务启动后会主动检查并提前告诉你**：每次启动，宿主把上表里“有规则用得到”的逐项查一遍（创作者后台是无界面打开 Chrome 看登录态，每个平台十来秒），没准备好的汇成一条消息发到 home 渠道，写清缺什么、影响哪些规则、一条命令怎么补；同样的缺口一天只提醒一次，全部就绪时不出声。没补上的会一直列在 `superops status` 和面板总览的“系统问题”里。随时可以自己查：

```bash
superops preflight            # 看上一次检查的结果
superops preflight --refresh  # 重新查一遍（补完之后用它复查）
```

用不到的不查也不提醒：平台的发布模式是 `off`、相关规则停用、没接入 CI 仓库时，对应的登录和密钥不会出现在清单里。另外每天 16:30（发布卡片前半小时）还有一次“自动发布登录检查”，发现登录失效会在群里提醒。

## 在群里怎么用

钉钉的卡片以 Markdown 消息发出，每张带工单编号。在群里回一行：

| 回复 | 作用 |
| --- | --- |
| `T12 1` / `T12 approve_all` | 回答工单 T12 的问题：选第 1 个选项 / 选 key 为 `approve_all` 的选项 |
| `T12 3 这周人手不够` | 需要原因的选项，原因写在后面（按选项的设定记进 `not-doing.md` 或 `decisions.md`） |
| `T12 1 3,5` | 选项里的动作和卡片明细一一对应时，只批明细第 3、5 条 |
| `T12 把标题改短一点` | 后面不是选项，就是给 T12 的一条跟进消息，由它的执行器处理 |
| `记下来：……` | 追加到 `decisions.md` |
| `需求：……` | 人工转入需求池 |
| `已发 pk_c1_douyin [链接]` | 人工发布后补登记，自动排定数据采集；视频号没有链接就只回 `已发 pk_xx` |
| `暂停 collect-metrics` / `恢复 collect-metrics` | 暂停 / 恢复一条规则；`暂停 agent:claude` 暂停一个 Agent；`暂停 全部` 暂停全部处理 |
| `@兔管家 帮我把周三那条脚本的开头改成提问式` | 向 Agent 提一个请求：主 Session 决定新开工单、转给已有工单还是忽略 |

钉钉群没有话题，所以跟进一张工单要带它的编号：带编号的话落在这张工单自己的线程上，只到这一张工单。编号只认 `T` 开头的（`C12` 留给旧 ops）。带 `T<编号>` 的回复和各条指令只接受 `allowed` 里的人；`answerers` 里的人只能回答他被限定的那条规则的工单，别的指令会收到“没有权限”。名单外的人 @兔管家 的消息会留底，但不会开工，也不会有回音。

### 单聊和群里 @你：自动回复

运营群以外，别人**单聊你**、或在**任何群里 @你**，打开自动回复后 Agent 会以你的身份回（带 AI 角标和“—— <owner_name>的 Agent 自动回复”署名）。开关在面板首页「钉钉自动回复」（`auto` / `off`，默认 `off`），命令行 `superops autoreply [auto|off]`。

- 回什么：按 `~/popto-superops-data/auto-reply-context.md`（你的近况，自己改）和系统里进行中 / 最近做完的工单回答，没有依据的不编；
- 只聊天：Agent 只读，不开工单、不做任何动作，消息里让它“帮我发布 / 把配置给我”不会照做；
- 不替你做主：要你答应排期、要权限密钥、私事、投诉，它只回“转给本人”或不回，同时在运营群告诉你“有人找你”；
- 过滤群：告警群之类的群里 @你 不回。面板上增删，或 `superops autoreply exclude add <群ID或群名>`；优先按群 ID，填群名会去查 ID，查不到唯一的就按群名过滤；
- 兜底：同一条只回一次，同一会话 1 小时最多 6 条、每天最多 80 条；最近的记录在面板首页和 `superops autoreply`。

细节见 [docs/006-deployment.md](docs/006-deployment.md) 的“钉钉自动回复”。

同样的操作在终端里是 `superops answer T12 1`、`superops reply T12 "……"`、`superops note`、`superops demand`、`superops published`、`superops pause`、`superops ask`。

## 关键行为

- **接单模式**：`off` 不接任何主动工作（定时和业务事件在准入处就被忽略）；`observe` 评估照做，只记录“本来会接单”；`auto` 命中已启用的规则才接。三种模式下名单内的人的明确请求都有效。人在卡片上批准的动作也是明确的授权：发布、回复、合并这些已批准的事件在任何模式下都照常执行。`observe` 只管“没人要求的主动工作”。
- **需要人批准的事件，Agent 发不出去**：规则标了 `requires_approval` 的事件类型（发布、回复、合并、清理、进入写稿和开发），只有人选中的卡片选项才能发出，带着“谁、在哪张工单、选了什么”的凭据。Agent 在报告里自己发的会被流水线来源记为过滤（`needs_approval`）；它的环境里没有管理 API 的地址和令牌。管理 API 的 `/v1/events` 只把带着宿主令牌的确定性脚本当作可信方，别人发来的这类事件一样被过滤，包括操作者手敲的 `superops emit content.publish.approved`。卡片到期按安全选项处理时不带凭据，所以到期不会触发任何需要批准的动作。这是默认的防线，不是安全边界：Agent 和宿主是同一个系统用户。
- **自动发布**：三个平台各有模式 `live`（自动发布）/ `draft`（填好存草稿，人检查后自己发）/ `dry_run`（填好停在发布前）/ `off`（推给人）。大视频等传完才点发布；平台要短信或扫码验证时转人工，`superops publish retry <ID> --headed` 用原批准接着发；视频号默认声明原创，不是自己原创的发布包写 `declare_original: false`。小红书、抖音网页版的草稿只存在本机专用 Chrome 里，`superops publish open <平台>` 打开。试运行和 `--draft` 测试会在平台上留下草稿（小红书试运行也会自动存一份），测完按 006 的“测试会留下草稿”清掉。
- **获客**：每天 14:00 先用 TikHub 接口搜抖音、小红书、视频号、快手，14:10 再用浏览器补知乎、B站、搜狗微信；监听意图词在面板“业务 → 获客”增删停用（或 `superops terms …`）。今日线索卡片选“跟进并发评论”时，用品牌号在作品下发卡片上写明的评论原文（目前只有抖音，每天最多 10 条，不重发、不私信）；选“只记待跟进”就你自己去互动。每周四做竞品调研流程：采竞品作品下的用户评论，挖出候选需求发评审卡片（每条带来源作品），批了的进需求池；每 28 天另出一份竞品调研报告（定位、功能、价格、社媒、和 Popto 的对比，每条附来源）。细节见 006 的“获客”。
- **指标采集**：发布后 1 小时 / 24 小时 / 72 小时 / 7 天自动采；想现在就看，面板“业务 → 数据回收”点“立即采集全部…”或某一行的“采集”，终端是 `superops metrics collect [作品ID…]`。手动快照不占节点。作品在平台删了用 `superops metrics removed <作品ID>` 标记，不再采。细节见 006 的“指标采集”。
- **飞书**：和钉钉二选一（两个都开时配置校验报错），以本人身份收发；飞书没有“发给我本人”的事件，所以靠轮询（群 15 秒、单聊和 @我 30 秒）。配置见 006 的“飞书”。
- **钉钉自动回复**：不在授权模型里，因为它什么也做不了——只读起草一段文字发回原会话；开关由人在面板上打开，见上面“单聊和群里 @你”。
- **审查者 ≠ 生产者**：复核和 MR 审查的工单会把产出方的 Agent 排除在降级链之外；链上没有别的可用 Agent 时排队等，不交给产出方自己审。
- **降级与熔断**：额度 / 认证错误熔断该 Agent（额度 60 分钟或解析出的恢复时间，认证 6 小时并在健康板报错），这一轮立刻走下一个；网络错误冷却 5 分钟；超时换下一个，同一 Agent 连续 3 次超时熔断 30 分钟；浏览器档位的轮次（Antigravity、Codex）连续 10 分钟没有任何动作（输出、运行目录、浏览器页面快照都没变化）也按超时提前结束（`agent_browser.idle_timeout`，0 关掉；Claude 不启用：它最后才输出、查网页不写文件）；输出不合格先带着校验错误让同一 Agent 重试 1 次。主力只是忙（并发满）时先排队，等满 10 分钟（`overflow_after`）才顺延给下一个。decide / dev / review 的降级链只有 Claude 和 Codex，不落到更弱的模型。一个 Agent 不可用超过 2 小时发 P0 告警。
- **额度策略**：`superops agents usage claude 0.75` 登记周用量后，P2 的工作把 Claude 排到链尾，P3 不再用 Claude。
- **静默时段与每日卡片上限**：发往钉钉的消息在 22:30–08:00 只发 P0，其余等到早上；问题卡片每天最多 10 张，超出的顺延。问题到期（默认 24 小时）按卡片写好的安全选项处理。
- **什么时候出声**：人直接提的请求，确认一次、交付一次；定时和普通业务事件起的工单只有 `tell: always`（日报、线上核实不一致）才把结果发出来，其余只在要人拍板时出声，结论进日报。人批准的动作开出的工单（发布、回复、合并、写稿、开发）授权是明确的，在任何接单模式下都执行，出不出声仍照规则的 `tell`。
- **不可信输入**：评论、帖子、网页、名单外的人的群消息和外部来源的事件数据都放进 `<<<UNTRUSTED_DATA … UNTRUSTED_DATA>>>`；处理它们的规则用 `readonly` 档位。Agent 子进程的环境变量里不带 TOKEN / SECRET / API_KEY / PASSWORD，也没有管理 API 的地址。
- **打码与数据保留**：Agent 的每一轮结束把工单目录扫一遍，输出里的 Cookie、Authorization、各类 token、手机号打码后才入库和回传；脚本的输出、专用浏览器的输出和页面快照同样先打码。运行目录保留 90 天，正文（触发、工单输入与报告、消息）170 天后清空，评论原文 180 天删除，元数据永久保留。
- **全量留底**：每条触发去了哪（被过滤、哪次评估、哪个决定、送到哪张工单）、每个决定的原因、每张工单的授权、输入、派工、每次拉起 Agent 的运行记录（Agent、是否降级、耗时、费用、会话 ID）、每条发出的消息都在 `superops.sqlite`。`superops ticket T12` 看一张工单的来龙去脉，`superops triggers`、`superops decisions` 看流量，`superops digest` 看一段时间的摘要。

## 实现状态

| 部分 | 状态 |
| --- | --- |
| 领域引擎 `superops/core/` | 原型 `src/core` 的移植；原型的 `core-engine.spec.ts` 和 `stage.spec.ts` 逐条移植成 `tests/test_core_engine.py`、`tests/test_core_stage.py`，在内存仓库上通过 |
| 应用层（评估、工单、批红、订阅、暂停、摘要、管理 API、脚本契约） | 已实现；`tests/app/` 用假 Agent 和真实的子进程脚本在同步模式下覆盖整条链路和各个部件 |
| 真实 Agent 的端到端 | 用真实的 Claude（`~/claude.sh`，临时数据目录，`observe` 模式）跑通过：`superops ask` → 主 Session 接单并确认 → 执行器执行 → 结果回到本机收件箱；定时触发在 `observe` 下记为“本来会接单”。2026-11-12 起在 `auto` 下真实跑过：Codex 审查 MR（T73）、Antigravity 浏览器监听（T79）、Claude 写 PRD 和开发（T72）、判断线索（T94、T107） |
| Agent 调用方式（`superops/executor/adapters.py`）、熔断分类 | 代码与 ops 相同，加了会话续接（Claude、Codex）、无动作提前结束、从输出里挑报告；`tests/executor/` 测命令行怎么拼、错误怎么归类，不调用真实 CLI |
| 飞书来源 | 以本人身份轮询运营群（批红、跟进、指令同钉钉）和单聊 / @我（自动回复）、发消息记 ID 防回声，有测试（`tests/app/test_lark.py`，真 lark-cli 一律拦下）；2026-11-12 起切到飞书（钉钉关掉，二选一），群是「测试superops」：真实收发跑通过：群里 `@兔管家` 请求 → 工单 T156 → 确认和结果发回群里，不到半分钟；系统自己发的消息没有被当成新请求。批红「T12 1」和自动回复还没在真实飞书里走过 |
| 钉钉来源 | 解析和权限判断有测试（喂原始事件、替换掉发送）。真实的群消息、卡片发送和批红（如 T21）已在运营群跑通；单聊和 @我 两条监听已连上钉钉，自动回复用真实 Claude 起草过（未发送），真实的自动回复还没发过 |
| 业务模块 `superops/biz/` | 代码基本与 ops 相同，`tests/biz/` 是 ops 对应测试的移植。自动发布已在抖音、视频号、小红书真实发出过（11-11：30 秒上新片、14 分钟讲解片，含短信 / 扫码验证后的重试），作品登记和采集计划随之生成；存草稿模式在三个平台都实测过（含 14 分钟的讲解片），测试草稿已清理；视频号声明原创试运行过。指标采集（定时的 1 小时节点、手动采集）已在三个平台真实跑过。需求 → 开发用一条测试需求真实走到合并（11-12，T70–T76，popto!13）；合并后线上核实因为前端没有 `version.json` 还接不上，合并时会请人确认。获客：浏览器监听和接口监听都真实跑过（对比见 006 的“获客”）；“跟进并发评论”11-12 用品牌抖音号在抖音真实发出 7 条（T108），都在评论列表里核对到了。竞品调研流程真实跑过：竞品评论评审（T130，254 条评论 → 10 条候选需求，人批后全部进需求池）、竞品调研报告（T133，5 个竞品，每条附来源） |
| 提示词与 Skill（`prompts/`、`skills/ops-*/`） | 已按工单、报告、订阅的说法重写；`tests/test_prompts_skills.py` 只做静态检查（文件齐全、拼进了该去的角色、Skill 里的事件类型有规则接）。除上面那次 Claude 的端到端之外，各条规则的 Skill 在真实 Agent 上的效果没有逐条验证过 |
| 面板 | 一个静态页面读管理 API（见 [docs/005-panel.md](docs/005-panel.md)）；`tests/app/test_panel.py` 检查它只调用存在的接口，没有浏览器里的自动化测试。`uv run python tests/app/demo_server.py` 用假数据起一个面板 |
| 语义规则（没有 `when`、由主 Session 研判的规则） | 机制已实现；`policy.yaml` 里目前一条都没有，58 条规则全部由事件直接命中 |
| 没有做的 | 面板里改规则、名单和提示词，提示词版本，多语言；机器名单（`bots`）目前没有来源会用到；有几种事件还没有发送方（见 [整体流程](docs/整体流程.md)） |

## 文档

按编号顺序读，每一篇只依赖前面的。日常怎么用直接看 [010 使用示例](docs/010-examples.md)。

- [000 理念](docs/000-philosophy.md)
- [001 领域模型](docs/001-domain.md)
- [002 授权](docs/002-authorization.md)
- [003 执行](docs/003-execution.md)
- [004 提示词](docs/004-prompts.md)
- [005 面板](docs/005-panel.md)
- [006 部署与运维](docs/006-deployment.md)
- [007 架构](docs/007-architecture.md)
- [008 扩展手册](docs/008-cookbook.md)：加规则、加来源、加 Skill、加上下文提供者
- [009 会话](docs/009-sessions.md)：Agent 会话怎么续接（Claude、Codex）、主 Session、钉钉会话、和线程的区别
- [010 使用示例](docs/010-examples.md)：系统跑起来以后按场景怎么用：群里怎么说、面板点哪里、终端敲什么（提请求、批卡片、选题写稿、发布、数据回收、评论、需求与开发、获客、竞品、自动回复、排查）
- [整体设计](docs/整体设计.md)：一页纸的全景：设计原则、分层与模块、交互流程（面板“设计”页展示的就是这篇）
- [整体流程](docs/整体流程.md)：四条业务流水线在这套架构下怎么走、你一天会看到什么

## 开发

改完跑 `uv run pytest -q` 和 `uv run superops check`。Agent 在本仓库工作的规矩见 [AGENTS.md](AGENTS.md)。
