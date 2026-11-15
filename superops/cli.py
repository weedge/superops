"""superops —— 运维命令。

  superops init                        初始化数据目录、数据库、local.yaml 模板
  superops doctor                      检查本机依赖（Agent CLI、dws、ffmpeg、钥匙串、策略、宿主是否在跑）
  superops check                       校验 policy.yaml / agents.yaml / schedules.yaml
  superops run [--once]                启动宿主（来源、评估、执行器、发件箱、管理 API 和面板）
  superops status                      有没有事要你处理、系统问题、Agent 状态
  superops tickets [--filter person|open|queued|working|idle|ended|all]
  superops ticket <T12>                一张工单的来龙去脉（授权、输入、派工、发出的消息）
  superops ask "……" [--thread X]       直接对 Agent 说一句话（明确请求）
  superops reply <T12> "……"            对一张工单跟进一句
  superops answer <T12> <序号或key> [原因]   回答工单的问题（批红）；「answer T4 1 3,5」只批明细第 3、5 条
  superops stop <T12> [原因] / retry <T12>
  superops triggers / decisions [--kind accept|ignore|unavailable|would_accept]
  superops reevaluate <trigger_id> / dismiss <decision_id>
  superops mode <off|observe|auto>     接单模式
  superops pause [规则|agent:名|origin:来源] / resume […]   不带参数 = 全部处理
  superops reload                      重新读 policy.yaml / local.yaml / schedules.yaml
  superops fire <cron.type>            手动补发一条定时触发
  superops emit <type> [--source S] [--subject X] [--summary …] [--data JSON] [--dedup K]
  superops note "……" / demand "……" / published <pk_id> [链接]
  superops terms add|enable|disable|remove <词> [--category pain|solution|competitor|brand|persona]   监听意图词（面板“业务 → 获客”同样能改）
  superops inbox [-n 20]               本机收件箱（没配钉钉时，问题和结果在这里）
  superops agents [reset|usage|open] … Agent 熔断状态
  superops digest [--hours 24]         留底摘要
  superops preflight [--refresh]       要提前登录的、要提前放好的密钥：哪些没准备好、影响什么、怎么补
  superops retention [--dry-run]       数据保留清理（定时规则也调它）
  superops metrics collect [作品ID…]    立即采集一遍指标（不带 ID 就是最近 30 天全部）；import <csv> / removed <作品ID…>
  superops publish login|check|try|retry|open … / browse install|login|check|sites …
  superops panel                       打开面板
  superops launchd install [--load] / skills link
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

from .infra import config as cfgmod
from .biz import db
from .infra import paths
from .biz.client import HubClient
from .infra.timeutil import iso, now

LOCAL_TEMPLATE = """# 本机配置（不进 git）。agents.yaml 和 policy.yaml 里的同名键会被这里深度覆盖。
proxy: {proxy}

{chat}
notifier:
  bot_name: 兔管家
  dingtalk:
    identity: user

# ---- 需求开发：建分支 → 推送 → 开 MR / PR → 审查 → 合并。GitLab 和 GitHub 可以同时接；superops doctor 的“开发与合并”逐项查
ci:
  gitlab_url: https://gitlab.example.com    # 接 GitLab 时填；令牌放钥匙串（doctor 里有命令）
  github_host: github.com   # 接 GitHub 时：先 gh auth login --hostname github.com --git-protocol https --web
  github_merge_method: merge  # merge | squash | rebase
  projects: []              # 例如 [popto/frontend_popto, "github:<owner>/<repo>"]；GitHub 写 github: 前缀或完整地址
  auto_merge: false         # 先都请人批
  base_branch: master       # GitLab 用；GitHub 仓库默认往仓库的默认分支合；按项目写成 {{<项目>: <分支>}}
  repos: {{}}                 # 项目 → 本机仓库路径（origin 要指向这个项目），例如 {{"<owner>/<repo>": ~/code/<repo>}}
  verify: {{}}                # 合并后核实线上版本：{{<项目>: {{url: https://…/version.json, within: 24h}}}}；没配的合并后请人确认上线
{github_example}
"""

GITHUB_EXAMPLE = """
# ---- 接 GitHub 仓库的例子：把上面 ci 里的 projects / repos 换成下面这样（去掉 # ），再跑 superops doctor 看“开发与合并”几行
#   projects: ["github:{owner}/<仓库>"]            # 也可以写 https://{host}/{owner}/<仓库>；GitLab 项目写在同一个列表里
#   repos: {{"{owner}/<仓库>": ~/code/<仓库>}}        # 本机 clone 的路径，origin 要指向这个仓库
#   base_branch: {{"{owner}/<仓库>": main}}           # 可不写：GitHub 仓库默认往仓库的默认分支合
# 准备（一次）：{prepare}
#   gh auth setup-git                               # git push 用 gh 的凭据（已能用钥匙串凭据推送的可跳过）
#   git clone https://{host}/{owner}/<仓库>.git ~/code/<仓库>"""


def client(timeout: float = 20) -> HubClient:
    hub = cfgmod.load_agents_config().get("hub") or {}
    return HubClient(f"http://{hub.get('host', '127.0.0.1')}:{hub.get('port', 8797)}", hub.get("token"), timeout=timeout)


def out(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def need_host(c: HubClient) -> HubClient:
    if not c.health():
        sys.exit("宿主没有运行：前台运行 `uv run superops run`，或 `superops launchd install --load`")
    return c


def call(method: str, path: str, body: dict | None = None, **params):
    c = need_host(client())
    try:
        return c.post(path, body or {}) if method == "POST" else c.get(path, **params)
    except RuntimeError as error:
        sys.exit(str(error))


def local(ts: str | None) -> str:
    """库里的时间（UTC 或带时区）→ 本地的「11-11 14:05」。"""
    from .core.model import parse_time
    d = parse_time(ts) if ts else None
    return d.astimezone().strftime("%m-%d %H:%M") if d else ""


def short(text: str | None, n: int = 60) -> str:
    text = (text or "").replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


# ---------------------------------------------------------------------- init / doctor / check
CHAT_LARK = """# ---- 找人的地方：飞书（以我本人身份收发：lark-cli --as user，先 lark-cli auth login）。钉钉和飞书只能开一个
# 群可以填群 ID（oc_ 开头）或完全一样且唯一的群名；查 ID：lark-cli im +chat-search --query <群名> --as user
home: {{source: lark, thread: home, address: {{chat: "{group}"}}}}
sources:
  lark:
    enabled: true
    chats: ["{group}"]
    observe: mention          # mention 只收 @兔管家 的和回复工单的 | all 群里所有消息
policy:
  sources:
    lark:
      allowed: {allowed}{allowed_note}
      answerers: {{}}           # 只能批某条规则的人：{{lead-card: ["ou_..."]}}
"""

CHAT_DINGTALK = """# ---- 找人的地方：钉钉。钉钉和飞书只能开一个
home: {{source: dingtalk, thread: home, address: {{chat: "{group}"}}}}
sources:
  dingtalk:
    enabled: true
    chats: ["{group}"]        # 群的 openConversationId：dws chat +chat-search --query <群名>
    observe: mention
policy:
  sources:
    dingtalk:
      allowed: []             # 能提请求、能批所有问题的人：[{{id: "<openDingTalkId>", name: "审批人"}}, {{id: "<userId>", name: "审批人"}}]
      answerers: {{}}
"""

CHAT_NONE = """# ---- 找人的地方：还没配聊天软件，问题和结果落在本机收件箱（superops inbox 查看）"""

CHAT_SWITCH_NOTE = """
# ---- 换成钉钉：把上面一段换成下面这样（钉钉和飞书只能开一个，home 也要跟着改）
# home: {source: dingtalk, thread: home, address: {chat: "<群 openConversationId>"}}
# sources:
#   lark: {enabled: false}
#   dingtalk: {enabled: true, chats: ["<群 openConversationId>"], observe: mention}
# policy:
#   sources:
#     dingtalk:
#       allowed: [{id: "<openDingTalkId 或 userId>", name: "审批人"}]   # 群消息带 openDingTalkId，卡片回调带 userId，两个都填
"""

WRAPPERS = ("~/claude.sh", "~/codex.sh", "~/agy.sh")


def wrapper_proxy_default() -> str | None:
    """包装脚本里写的代理（第一个写了的）：生成 local.yaml 时当全局代理，直调命令的 Agent 也走它。"""
    from .executor.adapters import wrapper_proxy
    return next((p for p in (wrapper_proxy(w) for w in WRAPPERS) if p), None)


def render_local(chat: str = "lark", group: str | None = None, proxy: str | None = None,
                 lark_probe=None, github_probe=None) -> str:
    """生成 local.yaml。飞书：lark-cli 登录着就把群名换成群 ID、把本人 open_id 填进审批人；没登录先写群名，启动后由来源去换。"""
    if chat == "lark":
        group = group or "测试superops"
        allowed, note = "[]", "             # 填你的 open_id（lark-cli auth status 的 userOpenId）：[{id: \"ou_...\", name: \"本人\"}]"
        me = (lark_probe or _probe_lark)(group)
        if me.get("chat_id"):
            group = me["chat_id"]
        if me.get("open_id"):
            allowed = f'[{{id: "{me["open_id"]}", name: "{me.get("user") or "本人"}"}}]'
            note = "   # 能提请求、能批所有问题的人：飞书 open_id（不认昵称）"
        block = CHAT_LARK.format(group=group, allowed=allowed, allowed_note=note) + CHAT_SWITCH_NOTE
    elif chat == "dingtalk":
        block = CHAT_DINGTALK.format(group=group or "<群 openConversationId>")
    else:
        block = CHAT_NONE + "\n" + CHAT_SWITCH_NOTE
    return LOCAL_TEMPLATE.format(proxy=f'"{proxy}"' if proxy else "null", chat=block.rstrip("\n"),
                                 github_example=github_example((github_probe or _probe_github)()))


def github_example(gh: dict) -> str:
    """local.yaml 里注释掉的 GitHub 示例：gh 登录着就用登录的账号当 owner，准备那步写“已登录”。"""
    host, owner = gh.get("host") or "github.com", gh.get("account") or "<owner>"
    prepare = (f"gh 已登录 {host}（{owner}），不用再登录" if gh.get("account")
               else f"brew install gh（没装的话）；gh auth login --hostname {host} --git-protocol https --web")
    return GITHUB_EXAMPLE.format(owner=owner, host=host, prepare=prepare)


def _probe_github(host: str = "github.com") -> dict:
    """gh 登录着的话：登录的账号（给示例当 owner）。没装、没登录都返回只有 host 的。"""
    if not shutil.which("gh"):
        return {"host": host}
    from .biz.ci.checks import gh_auth
    st = gh_auth(host)
    return {"host": host, "account": st.get("account") if st.get("ok") else None}


def _probe_lark(group: str) -> dict:
    """lark-cli 登录着的话：本人 open_id、群名对应的群 ID。没装、没登录、查不到都返回空的。"""
    if not shutil.which("lark-cli"):
        return {}
    from .infra.notifier import lark
    status = lark.auth_status()
    if not status.get("ok"):
        return {}
    out = {"open_id": status.get("open_id"), "user": status.get("user")}
    if not group.startswith("oc_"):
        try:
            out["chat_id"] = lark.find_chat(group)
        except lark.LarkError:
            pass
    return out


def cmd_init(a) -> None:
    root = paths.ensure_data_dirs()
    db.open_ops().close()
    from .app.store import Store
    Store(root / "superops.sqlite").close()
    local = root / "local.yaml"
    if getattr(a, "chat", "lark") == "dingtalk" and not getattr(a, "group", None):
        sys.exit("--chat dingtalk 要同时给 --group <群 openConversationId>（dws chat +chat-search --query <群名>）")
    if local.exists() and getattr(a, "reset_local", False):
        backup = local.with_name(f"local.yaml.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        local.rename(backup)
        print(f"原来的 local.yaml 备份到 {backup}")
    if not local.exists():
        local.write_text(render_local(getattr(a, "chat", "lark"), getattr(a, "group", None), wrapper_proxy_default()), encoding="utf-8")
        local.chmod(0o600)
    print(f"数据目录：{root}\n本机配置：{local}\n下一步：superops doctor；接单模式默认是 observe（只记录“本来会接单”），看一阵没问题再 `superops mode auto`。")


def check() -> tuple[list[str], list[str]]:
    """校验配置：返回 (错误, 提醒)。"""
    import yaml

    from .app.settings import deep_merge, normalize
    from .biz.context import CONTEXT_PROVIDERS
    from .infra.instance import current as current_instance
    errors, warnings = [], []
    home = paths.ops_home()
    try:   # 数据目录（也就是 local.yaml 在哪）由 instance 决定，要先过这一关
        inst = current_instance(home)
    except (ValueError, yaml.YAMLError) as error:
        return [f"agents.yaml 的 instance 写错了：{error}"], []
    if not inst.name:
        warnings.append(f"agents.yaml 没写 instance.name：数据目录用 {inst.data_dir}，钥匙串服务名用 {inst.keychain_service}/<密钥名>")
    try:
        cfg = cfgmod.load_agents_config(home)
    except Exception as error:  # noqa: BLE001
        return [f"agents.yaml / local.yaml 读不了：{error}"], []
    try:
        base = yaml.safe_load((home / "policy.yaml").read_text(encoding="utf-8")) or {}
        local_path = paths.ops_data() / "local.yaml"
        local = (yaml.safe_load(local_path.read_text(encoding="utf-8")) or {}) if local_path.exists() else {}
        settings = normalize(deep_merge(base, {k: local[k] for k in ("eval", "executor", "sources", "policy", "home") if k in local}))
    except Exception as error:  # noqa: BLE001
        return [f"policy.yaml 不合法：{error}"], []
    agents, chains = cfg.get("agents") or {}, cfg.get("fallback_chains") or {}
    if settings["eval"].get("role") not in chains:
        errors.append(f"主 Session 的角色 {settings['eval'].get('role')}（policy.yaml 的 eval.role）在 agents.yaml 的 fallback_chains 里没有")
    for name, ac in agents.items():      # 模型和思考强度：角色要在降级链里，强度要是认识的档位；不调用模型的 Agent 不该配
        tuned = ac.get("models") or {}
        for role, t in tuned.items():
            if role not in chains:
                errors.append(f"Agent {name}：models 里的角色 {role} 在 fallback_chains 里没有")
            if not isinstance(t, dict):
                errors.append(f"Agent {name}：models.{role} 要写成 {{model, effort}}")
            elif t.get("effort") and t["effort"] not in cfgmod.EFFORT:
                errors.append(f"Agent {name}：models.{role}.effort 是 {t['effort']}，只认 {'/'.join(sorted(cfgmod.EFFORT))}")
        if ac.get("default_effort") and ac["default_effort"] not in cfgmod.EFFORT:
            errors.append(f"Agent {name}：default_effort 是 {ac['default_effort']}，只认 {'/'.join(sorted(cfgmod.EFFORT))}")
        if ac.get("type") in ("script", "inbox") and (tuned or ac.get("default_model")):
            warnings.append(f"Agent {name}：{'脚本' if ac['type'] == 'script' else '收件箱'}不调用模型，models / default_model 不会生效")
        for role, chain in chains.items():
            if name in (chain or []) and ac.get("type") not in ("script", "inbox") and tuned and role not in tuned and not ac.get("default_model"):
                warnings.append(f"Agent {name} 在 {role} 的降级链里，但 models 没配这个角色、也没有 default_model：会用 CLI 自己的默认模型")
    always = 0
    for rule in settings["policy"]["rules"]:
        run = rule["run"]
        if run["executor"] == "agent":
            role = run.get("role")
            if role not in chains:
                errors.append(f"规则 {rule['id']}：角色 {role} 在 agents.yaml 的 fallback_chains 里没有")
            if run.get("agent") and run["agent"] not in agents:
                errors.append(f"规则 {rule['id']}：Agent {run['agent']} 没有配置")
            if run.get("skill") and not cfgmod.find_skill(run["skill"], cfg, home):
                (errors if rule["enabled"] else warnings).append(f"规则 {rule['id']}：找不到 Skill {run['skill']}")
            for provider in run.get("context") or []:
                if provider not in CONTEXT_PROVIDERS:
                    errors.append(f"规则 {rule['id']}：不认识的上下文 {provider}")
        if rule["enabled"] and rule["tell"] == "always" and run["executor"] != "none":
            always += 1
        if rule["requires_approval"] and run["executor"] == "agent" and run.get("tools") == "readonly":
            warnings.append(f"规则 {rule['id']}：需要人批准的事件交给了只读的 Agent，确认是不是应该用脚本执行器")
    if always > int(cfg.get("max_tell_always_rules", 4)):
        errors.append(f"tell=always 的规则有 {always} 条，超过上限 {cfg.get('max_tell_always_rules', 4)}：每条都会打扰人")
    types = {t for r in settings["policy"]["rules"] if r.get("when") for t in r["when"]["types"]}
    for spec in cfgmod.load_schedules(home) or []:
        if spec.get("type") not in types:
            warnings.append(f"定时 {spec.get('type')} 没有任何规则接")
        if not spec.get("name"):
            warnings.append(f"定时 {spec.get('type')} 没写 name：面板上没有中文名")
    home_source = settings["home"].get("source")
    if home_source not in settings["sources"] or not settings["sources"][home_source]["enabled"]:
        errors.append(f"home 渠道的来源 {home_source} 没有启用：主动工单的问题和结果会发不出去")
    for name, label in (("dingtalk", "钉钉"), ("lark", "飞书")):
        if settings["sources"].get(name, {}).get("enabled") and not (settings["policy"]["sources"].get(name) or {}).get("allowed"):
            warnings.append(f"{label}来源开着，但名单（policy.sources.{name}.allowed）是空的：没有人能在群里提请求或批红")
    return errors, warnings


def cmd_check(a) -> None:
    errors, warnings = check()
    for line in errors:
        print("✗", line)
    for line in warnings:
        print("!", line)
    print(f"{len(errors)} 个错误，{len(warnings)} 个提醒")
    if errors:
        sys.exit(1)
    if client().health():
        print("宿主在运行：`superops reload` 让改动生效")


def cmd_doctor(a) -> None:
    cfg = cfgmod.load_agents_config()
    rows = []
    from .executor.adapters import COMMANDS, adapter_for, direct_bin, uses_wrapper, wrapper_proxy
    search = ":".join(os.path.expanduser(p) for p in str(cfg.get("path", "")).split(":") if p)
    for name, ac in (cfg.get("agents") or {}).items():
        if ac.get("type") not in COMMANDS:          # 收发件箱、脚本、通用命令：不用装命令行
            rows.append((f"Agent {name}", True, ac.get("type")))
            continue
        b, cmd = ac.get("bin"), direct_bin(ac, search)   # 装没装看命令本体，和池子的判断一致
        ok = adapter_for(ac).available(ac, search)
        wanted = ac.get("command") or COMMANDS[ac["type"]]
        note = cmd or f"找不到命令 {wanted if isinstance(wanted, str) else ' / '.join(wanted)}"
        if b and uses_wrapper(ac):
            note += f"；经包装脚本 {b}（代理 {ac.get('proxy') or wrapper_proxy(b) or '脚本内未写'}）"
        elif b:
            note += f"；包装脚本 {b} 不在，直接调用命令（代理 {ac.get('proxy') or cfg.get('proxy') or '未配置'}）"
        if ac.get("strict_tiers"):
            note += f"；{'/'.join(ac['strict_tiers'])} 档位直调命令本体"
        rows.append((f"Agent {name}", ok, note))
    rows += chat_rows()
    rows += dev_rows(cfg)
    for tool in ("ffmpeg", "whisper-cli", "uv"):
        rows.append((tool, bool(shutil.which(tool)), shutil.which(tool) or "未找到"))
    from .infra.secrets import add_command, keychain, service_for
    for k in ("tikhub", "redfox"):                 # GitLab 令牌在“开发与合并”里查（接了 GitLab 仓库才要）
        rows.append((f"钥匙串 {service_for(k)}", bool(keychain(k)), "已配置" if keychain(k) else add_command(k)))
    from .biz import agent_browser as ab
    abs_ = ab.settings(cfg)
    if abs_["enabled"]:
        ver = ab.cli_version(cfg)
        rows.append(("专用浏览器 playwright-cli", bool(ver), ver or f"不可用：{' '.join(abs_['cli'])}"))
        rows.append(("专用浏览器登录态", ab.is_initialized(abs_), str(abs_["profile"]) if ab.is_initialized(abs_)
                     else "还没登录过：superops browse login"))
    errors, warnings = check()
    rows.append(("策略与配置校验", not errors, f"{len(errors)} 个错误，{len(warnings)} 个提醒（superops check 看详情）"))
    c = client()
    up = c.health()
    rows.append(("宿主", up, c.base))
    if up:
        overview = c.get("/api/overview")
        rows.append(("接单模式", True, overview["mode"] + ("（已暂停处理）" if overview["paused"] else "")))
        for source in c.get("/api/sources"):
            rows.append((f"来源 {source['title']}", source["state"] != "error", f"{source['state']} {source['detail']}"))
        for problem in overview["problems"]:
            rows.append((f"问题 {problem['title']}", False, problem["detail"]))
    for name, ok, info in rows:
        print(f"{'✓' if ok else '✗'} {pad(name, 26)} {info}")


def pad(text: str, width: int) -> str:
    """按显示宽度补空格（中文占两格），让中英文混排的列对齐。"""
    import unicodedata
    used = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(1, width - used)


class _NoOverrides:
    def get(self, key, default=None):
        return default


def effective_settings() -> dict:
    """宿主实际用的设置：policy.yaml 叠 local.yaml，再叠面板在库里改的（聊天渠道、模式、暂停）。不用宿主在跑。"""
    from .app.settings import Settings
    from .app.store import Store
    db_path = paths.ops_data() / "superops.sqlite"
    store = Store(db_path) if db_path.exists() else None
    try:
        return Settings(store or _NoOverrides(), None).current()
    finally:
        if store:
            store.close()


def dev_rows(cfg: dict) -> list[tuple[str, bool, str]]:
    """doctor 的开发与合并几行：git、GitLab 令牌 / GitHub 的 gh 登录和推送凭据、每个仓库的权限和本机路径。"""
    from .biz.ci import checks
    try:
        found = checks.check(cfg)
    except Exception as error:  # noqa: BLE001
        return [("开发与合并", False, f"没查成：{str(error)[:160]}")]
    return [(r["title"], r["ok"], r["detail"] + (f" → {r['fix']}" if r.get("fix") else "")) for r in found]


def chat_rows() -> list[tuple[str, bool, str]]:
    """doctor 的聊天软件几行：钉钉 / 飞书命令行、登录、群、home、审批人。"""
    from .app import chatcheck
    try:
        settings = effective_settings()
    except Exception as error:  # noqa: BLE001  配置写错：check 那一行会报详情
        return [("聊天渠道", False, f"配置读不了：{str(error)[:160]}")]
    return [(r["title"], r["ok"], r["detail"] + (f" → {r['fix']}" if r.get("fix") else "")) for r in chatcheck.check(settings)]


# ---------------------------------------------------------------------- run
def bind_with_retry(make, *, tries: int = 60, wait: float = 2.0):
    """绑端口。上一个进程还在退出（等手头的工单收尾）时端口会被占一会儿：每 wait 秒重试，最多 tries 次，
    还不行就抛出去。2026-11-12 launchd 重启时新进程绑端口失败、又被后台线程拖住，卡成了“有监听没面板”的半死状态。"""
    import errno
    for attempt in range(tries):
        try:
            return make()
        except OSError as e:
            if e.errno != errno.EADDRINUSE or attempt == tries - 1:
                raise
            if attempt == 0:
                logging.warning("端口还被占着（多半是上一个进程还在退出），每 %.0f 秒重试一次……", wait)
            time.sleep(wait)


def cmd_run(a) -> None:
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    errors, warnings = check()
    for line in warnings:
        logging.warning("配置提醒：%s", line)
    if errors:
        sys.exit("配置有错误，没有启动：\n" + "\n".join(f"  ✗ {e}" for e in errors))
    from .app.http import serve
    from .app.runtime import Runtime
    rt = Runtime(sync=a.once)
    rt.echo = True
    rt.start()
    if a.once:  # 一次性：处理完手头的就退出（调试用）
        rt.pump()
        rt.stop()
        return
    try:
        server = bind_with_retry(lambda: serve(rt))
    except OSError as e:
        logging.error("管理端口绑不上，没有启动：%s", e)
        rt.stop()
        os._exit(1)   # 后台线程（监听、执行器）不是守护线程：不硬退会卡在退出流程里，launchd 也不会重启
    logging.info("superops 已启动：%s（面板同址）；接单模式 %s；数据目录 %s", rt.api_url, rt.settings.current()["eval"]["mode"], rt.data)
    stop = {"flag": False}

    def term(*_):
        stop["flag"] = True
    signal.signal(signal.SIGTERM, term)
    signal.signal(signal.SIGINT, term)
    while not stop["flag"]:
        time.sleep(0.5)
    logging.info("正在停止……")
    server.shutdown()
    server.server_close()   # 先把端口放掉：rt.stop() 要等手头的工单收尾，新进程这时就能起来
    rt.stop()


# ---------------------------------------------------------------------- 查询
def print_tickets(rows: list[dict]) -> None:
    if not rows:
        print("（没有）")
    for r in rows:
        extra = r.get("question") or (r.get("report") or {}).get("summary") or ""
        print(f"{r['code']:<5} {r['label']:<8} {short(r['title'], 34):<36} {short(r.get('rule') or '明确请求', 14):<16} {local(r['updated_at'])}  {short(extra, 50)}")


def cmd_status(a) -> None:
    o = call("GET", "/api/overview")
    print(f"接单模式 {o['mode']}{'（已暂停处理）' if o['paused'] else ''} · 策略版本 {o['policy_version']} · "
          f"进行中 {o['open']['total']} 张（等人 {o['open']['person']} / 工作中 {o['open']['working']} / 等事件 {o['open']['idle']}）· "
          f"执行位 {o['executor']['running']}/{o['executor']['max']}")
    if o["paused_keys"]:
        print("单独暂停：" + "、".join(o["paused_keys"]))
    print("\n== 等你处理 ==")
    print_tickets(o["needs_person"])
    if o["problems"]:
        print("\n== 系统问题 ==")
        for p in o["problems"]:
            print(f"✗ {p['title']}：{short(p['detail'], 120)}")
    flow = o["flow"]
    print(f"\n过去 24 小时：触发 {sum(flow['triggers'].values())}（{', '.join(f'{k} {v}' for k, v in flow['triggers'].items()) or '无'}）· "
          f"决定 {', '.join(f'{k} {v}' for k, v in flow['decisions'].items()) or '无'} · 本来会接单 {flow['would_accept']} · "
          f"新工单 {flow['tickets_created']} · 结束 {flow['tickets_ended']}")
    bad = {n: s for n, s in o["agents"].items() if s.get("state") != "closed" or not s.get("installed")}
    for name, s in bad.items():
        print(f"Agent {name}：{'本机没有' if not s.get('installed') else s.get('state')} {s.get('kind') or ''} {s.get('open_until') or ''}")


def cmd_tickets(a) -> None:
    print_tickets(call("GET", "/api/tickets", filter=a.filter, limit=a.limit))


def cmd_ticket(a) -> None:
    t = call("GET", f"/api/tickets/{a.ticket}")
    if a.json:
        return out(t)
    print(f"{t['code']} {t['title']}\n状态：{t['label']} · 规则：{t.get('rule') or '明确请求'} · 强度：{t['strength']} · 策略版本 {t['policy_version']}")
    print(f"范围：{t.get('scope') or ''}\n目录：{t.get('directory')}")
    if t.get("origin"):
        print(f"起点：[{t['origin']['source']}] {short(t['origin']['text'], 100)}")
    if t.get("decision"):
        print(f"决定：{t['decision']['label']}（{t['decision']['reason']}）")
    if t.get("question"):
        print(f"\n在等你回答：{t['question']}")
        for i, o in enumerate(t["options"], 1):
            print(f"  {i}. {o['label']}（{o['key']}）{' + 原因' if o['require_reason'] else ''}")
        print(f"  → superops answer {t['code']} <序号> [原因]")
    print("\n派工：")
    for x in t["assignments"]:
        rep = x.get("report") or {}
        print(f"  {local(x['created_at'])} {x['state']:<8} {rep.get('state', ''):<8} {short(rep.get('summary'), 90)}")
    print("输入：")
    for x in t["inputs"][-15:]:
        print(f"  {local(x['at'])} {x['kind']:<8}{'*' if x['waiting'] else ' '} {short(x.get('text'), 90)}")
    print("发出的消息：")
    for x in t["messages"][-10:]:
        print(f"  {local(x['at'])} {x['kind']:<15} {x['status']:<8} {short(x['text'], 80)}")
    print("运行：")
    for x in t["runs"][:10]:
        print(f"  {local(x['started_at'])} {x['role']:<8} {x['agent']:<12} {x['status']:<8} {x.get('duration_s') or ''}s {short(x.get('error_detail'), 60)}")


def cmd_triggers(a) -> None:
    for t in call("GET", "/api/triggers", limit=a.limit, source=a.source):
        print(f"{local(t['at'])} {t['source']:<9} {short(t.get('type') or ('人' if t['human'] else ''), 26):<28} {short(t['text'], 40):<42} → {short(t['went'], 60)}  {t['id'][:10]}")


def cmd_decisions(a) -> None:
    for d in call("GET", "/api/decisions", kind=a.kind, limit=a.limit):
        tail = f" → {d['ticket']['code']}" if d.get("ticket") else ""
        print(f"{local(d['at'])} {d['label']:<4} {d['strength']:<8} {short(d['thread'], 34):<36} {short(d['reason'], 70)}{tail}  {d['id'][:10]}")


def cmd_inbox(a) -> None:
    log = paths.ops_data() / "notify" / "console.log"
    if not log.exists():
        return print("（收件箱是空的）")
    for line in log.read_text(encoding="utf-8").splitlines()[-a.n:]:
        m = json.loads(line)
        print(f"── {local(m['at'])} [{m['kind']}]\n{m['text']}\n")


def cmd_agents(a) -> None:
    if a.action == "reset":
        out(call("POST", "/api/agents", {"agent": a.agent, "action": "reset"}))
    elif a.action == "usage":
        out(call("POST", "/api/agents", {"agent": a.agent, "action": "usage", "ratio": float(a.value)}))
    elif a.action == "open":
        out(call("POST", "/api/agents", {"agent": a.agent, "action": "open", "for": a.value or "30m"}))
    else:
        for name, s in call("GET", "/api/overview")["agents"].items():
            print(f"{name:<12} {s.get('state', 'closed'):<9} 并发 {s['busy']}/{s['concurrency']} {'' if s['installed'] else '本机没有 '}"
                  f"{'已暂停 ' if s.get('paused') else ''}{s.get('kind') or ''} {s.get('open_until') or ''} {short(s.get('last_error'), 60)}")


def cmd_preflight(a) -> None:
    """服务在跑就问它（--refresh 让它重查并把缺口发到 home 渠道）；没在跑就在本机直接查。"""
    c = client(timeout=180)
    if c.health():
        result = c.get("/api/preflight", refresh="1" if a.refresh else None)
    else:
        from .app import preflight
        from .app.settings import deep_merge, normalize
        import yaml
        home = paths.ops_home()
        base = yaml.safe_load((home / "policy.yaml").read_text(encoding="utf-8")) or {}
        local_path = paths.ops_data() / "local.yaml"
        overrides = (yaml.safe_load(local_path.read_text(encoding="utf-8")) or {}) if local_path.exists() else {}
        settings = normalize(deep_merge(base, {k: overrides[k] for k in ("eval", "executor", "sources", "policy", "home") if k in overrides}))
        result = preflight.run(cfgmod.load_agents_config(home), settings, logins=True)
    for item in result["items"]:
        print(f"{'✓' if item['ok'] else '✗'} {item['title']:<22} {item['detail']}")
        if not item["ok"]:
            print(f"    影响：{'、'.join(item['rules'])}\n    补：{item['fix']}")
    if not result.get("logins_checked"):
        print("（这次没查创作者后台的登录态：加 --refresh 重查）")
    print(f"检查时间 {local(result['at'])} · {len(result['missing'])} 项没准备好")
    if result["missing"]:
        sys.exit(1)


def cmd_retention(a) -> None:
    """数据保留清理：和宿主共用同一个库（SQLite WAL），所以可以单独跑，也是 data-retention 规则的脚本。"""
    from .app.store import Store
    from .biz import retention
    from .core.model import shift, now_iso
    store = Store(paths.ops_data() / "superops.sqlite")
    live = {Path((t.get("execution") or {}).get("directory") or "").name for t in store.tickets.open()}

    def is_active(kind: str, name: str) -> bool:
        return name in live

    def store_cleanup(text_days: int, dry_run: bool) -> dict:
        return store.blank_text(shift(now_iso(), -text_days * 86400), dry_run)
    result = retention.run(dry_run=a.dry_run, store_cleanup=store_cleanup, is_active=is_active)
    store.close()
    if os.environ.get("OPS_TASK_FILE"):  # 被宿主当脚本拉起：最后一行输出结果 JSON
        print(json.dumps({"status": "done", "summary": result["summary"][:600], "needs_decision": False, "decision_card": None,
                          "follow_up_events": [], "dogfood_friction": []}, ensure_ascii=False))
    else:
        print(result["summary"])


# ---------------------------------------------------------------------- 业务命令（原样带过来）
def cmd_metrics(a) -> None:
    from .biz.collectors.metrics import main as mmain
    if not a.args and a.action in ("import", "removed"):
        sys.exit(f"superops metrics {a.action} 要带{'CSV 文件' if a.action == 'import' else '作品 ID（posts.id，面板“数据”页能看到）'}")
    sys.exit(mmain([a.action, *a.args]))


def cmd_publish(a) -> None:
    from .biz.executors import publishers as pubs
    if a.action == "retry":
        from .biz.executors.publish import retry
        result = retry(a.platform, headed=a.headed)
        print(("✓ " if result["ok"] else "✗ ") + (result["note"] if result.get("draft") else
                                                  f"已发布，作品已登记：{result['post']['post_id']}" if result["ok"] else result["error"]))
        sys.exit(0 if result["ok"] else 1)
    if a.action == "try":
        from .biz.executors.publish import _pack_meta
        ops = db.open_ops()
        row = ops.execute("SELECT * FROM publish_packs WHERE id=?", (a.platform,)).fetchone()
        if not row:
            sys.exit(f"找不到发布包 {a.platform}")
        p = dict(row)
        res = pubs.run(p, _pack_meta(p["pack_dir"]), dry_run=not (a.live or a.draft), draft=a.draft, headless=not a.headed)
        print(("✓ " if res.ok else "✗ ") + ("试运行" if res.dry_run else "存草稿" if res.draft else "发布") + "：" + "；".join(res.notes))
        for s in res.screenshots:
            print("  截图", s)
        if res.url:
            print("  链接", res.url)
        return
    # 给的是账号键就只动这一个号（主号的账号键就是平台名，面板“校验账号”也这样调）；all 是全部号
    targets = [k for pf in pubs.ACCOUNTS for k in pubs.account_keys(pf)] if a.platform == "all" else [a.platform]
    if any(not pubs.is_account_key(k) for k in targets):
        sys.exit(f"不认识的账号 {a.platform}（平台名就是主号：{' / '.join(pubs.ACCOUNTS)}；后加的号如 douyin@2）")
    for pf in targets:
        label = pubs.account_label(pf)
        if a.action == "open":
            if pubs.platform_of(pf) not in pubs.DRAFTS:
                print(f"{label}：没有可打开的草稿箱")
                continue
            print(f"{label}：已打开专用 Chrome，停在草稿位置（{pubs.DRAFTS[pubs.platform_of(pf)]['where']}）。检查完点发布，关掉窗口即结束。")
            pubs.open_drafts(pf)
        elif a.action == "login":
            print(f"{label}：已打开 Chrome，请扫码登录（5 分钟内）……")
            ok = pubs.login(pf)
            print(f"{label}：{'登录成功，登录态已保存' if ok else '超时未登录'}")
        elif a.action == "qrlogin":
            from .biz.executors.publishers import qrlogin
            print(f"{label}：二维码会存到 {qrlogin.qr_file(pf)}（面板“内容 → 账号”里直接显示），用手机扫码……", flush=True)
            r = qrlogin.run(pf)
            st = qrlogin.read_status(pf)
            print(f"{'✓' if r.get('logged_in') else '✗'} {label} {st.get('message') or ('登录成功' if r.get('logged_in') else '没登录成功')}")
            print(json.dumps({"account": {k: r.get(k) for k in ("logged_in", "name", "id", "followers")}}, ensure_ascii=False))
            if not r.get("logged_in"):
                sys.exit(1)
        else:
            from .biz.executors.publishers import qrlogin
            r = qrlogin.whoami(pf)
            ok = r["logged_in"]
            who = f"  {r['name']}" if r.get("name") else ""
            print(f"{'✓' if ok else '✗'} {pf} {label} {'已登录' if ok else '未登录（superops publish qrlogin ' + pf + '）'}{who}  {r['url']}")
            print(json.dumps({"account": {k: r.get(k) for k in ("logged_in", "name", "id", "followers")}}, ensure_ascii=False))


def cmd_browse(a) -> None:
    from .biz import agent_browser as ab
    cfg = cfgmod.load_agents_config()
    s = ab.settings(cfg)
    if a.action == "sites":
        for k, u in ab.LOGIN_SITES.items():
            print(f"{k:<12} {u}")
        return
    if a.action == "install":
        sys.exit(ab.install(a.proxy))
    if a.action == "check":
        ver = ab.cli_version(cfg)
        print(f"{'✓' if ver else '✗'} playwright-cli  {ver or '不可用'}  {' '.join(s['cli'])}")
        print(f"{'✓' if ab.is_initialized(s) else '✗'} 登录态目录     {s['profile']}"
              + ("" if ab.is_initialized(s) else "（还没登录过：superops browse login）"))
        return
    urls = []
    for t in a.targets or list(ab.LOGIN_SITES):
        if t in ab.LOGIN_SITES:
            urls.append(ab.LOGIN_SITES[t])
        elif t.startswith("http"):
            urls.append(t)
        else:
            sys.exit(f"不认识的站点 {t}（superops browse sites 看清单，或直接给网址）")
    sys.exit(ab.login(cfg, urls))


def launchd_jobs() -> dict[str, list[str]]:
    """launchd 标签 → 命令。标签来自 agents.yaml 的 instance.launchd_label。"""
    from .infra.instance import current
    return {current().launchd_label: ["superops", "run"]}


def cmd_launchd(a) -> None:
    tpl = (paths.code_root() / "launchd" / "template.plist").read_text(encoding="utf-8")   # 模板和 --project 跟代码走，配置目录跟 SUPEROPS_HOME 走
    uv = shutil.which("uv") or str(Path("~/.local/bin/uv").expanduser())
    data = paths.ensure_data_dirs()
    agents_dir = Path("~/Library/LaunchAgents").expanduser()
    cfg = cfgmod.load_agents_config()
    path_env = ":".join(os.path.expanduser(p) for p in str(cfg.get("path", "")).split(":") if p)
    for label, argv in launchd_jobs().items():
        args = [uv, "run", "--project", str(paths.code_root()), *argv]
        xml = (tpl.replace("{{LABEL}}", label)
                  .replace("{{ARGS}}", "\n".join(f"    <string>{x}</string>" for x in args))
                  .replace("{{PATH}}", path_env).replace("{{OPS_DATA}}", str(data))
                  .replace("{{OPS_HOME}}", str(paths.ops_home())).replace("{{NAME}}", label.split(".")[-1]))
        dst = agents_dir / f"{label}.plist"
        if a.print:
            print(xml)
            continue
        dst.write_text(xml, encoding="utf-8")
        print(f"已生成 {dst}")
        if a.load:
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)
            r = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(dst)], capture_output=True, text=True)
            print(f"  加载 {'成功' if r.returncode == 0 else '失败：' + r.stderr.strip()}")
    if not a.load and not a.print:
        print("未加载。确认 local.yaml 配好后运行：superops launchd install --load")


def cmd_skills_link(a) -> None:
    targets = [Path("~/.claude/skills").expanduser(), paths.ops_home() / ".claude" / "skills", paths.ops_home() / ".agents" / "skills"]
    for t in targets:
        t.mkdir(parents=True, exist_ok=True)
        for sk in sorted((paths.ops_home() / "skills").glob("ops-*")):
            dst = t / sk.name
            if dst.is_symlink() or dst.exists():
                if dst.is_symlink() and dst.resolve() == sk.resolve():
                    continue
                print(f"跳过（已存在）：{dst}")
                continue
            dst.symlink_to(sk)
            print(f"{dst} → {sk}")


# ---------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="superops", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    post = lambda path, body: out(call("POST", path, body))  # noqa: E731
    get = lambda path: out(call("GET", path))  # noqa: E731
    text = lambda a: " ".join(a.text)  # noqa: E731
    who = os.environ.get("USER", "operator")
    s = sp.add_parser("init", help="建数据目录和库；没有 local.yaml 时生成一份")
    s.add_argument("--chat", choices=["lark", "dingtalk", "none"], default="lark", help="找人用哪个聊天软件（默认飞书）")
    s.add_argument("--group", help="群名或群 ID（飞书默认「测试superops」）")
    s.add_argument("--reset-local", action="store_true", help="已有 local.yaml 时先备份成 local.yaml.bak-<时间> 再重新生成")
    s.set_defaults(fn=cmd_init)
    sp.add_parser("doctor").set_defaults(fn=cmd_doctor)
    sp.add_parser("check").set_defaults(fn=cmd_check)
    s = sp.add_parser("run")
    s.add_argument("--once", action="store_true", help="处理完手头的就退出（调试用）")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_run)
    sp.add_parser("status").set_defaults(fn=cmd_status)
    s = sp.add_parser("tickets")
    s.add_argument("--filter", default="open", choices=["person", "open", "queued", "working", "idle", "ended", "all"])
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(fn=cmd_tickets)
    s = sp.add_parser("ticket")
    s.add_argument("ticket")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_ticket)
    s = sp.add_parser("ask")
    s.add_argument("text", nargs="+")
    s.add_argument("--thread")
    s.set_defaults(fn=lambda a: post("/api/ask", {"text": text(a), "thread": a.thread}))
    s = sp.add_parser("reply")
    s.add_argument("ticket")
    s.add_argument("text", nargs="+")
    s.set_defaults(fn=lambda a: post(f"/api/tickets/{a.ticket}/reply", {"text": text(a)}))
    s = sp.add_parser("answer")
    s.add_argument("ticket")
    s.add_argument("choice")
    s.add_argument("reason", nargs="*")
    s.set_defaults(fn=lambda a: post(f"/api/tickets/{a.ticket}/answer", {"choice": a.choice, "reason": " ".join(a.reason) or None, "by": who}))
    s = sp.add_parser("stop")
    s.add_argument("ticket")
    s.add_argument("reason", nargs="*")
    s.set_defaults(fn=lambda a: post(f"/api/tickets/{a.ticket}/stop", {"reason": " ".join(a.reason) or None}))
    s = sp.add_parser("retry")
    s.add_argument("ticket")
    s.set_defaults(fn=lambda a: post(f"/api/tickets/{a.ticket}/retry", {}))
    s = sp.add_parser("triggers")
    s.add_argument("--source")
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(fn=cmd_triggers)
    s = sp.add_parser("decisions")
    s.add_argument("--kind", choices=["accept", "ignore", "stop", "unavailable", "would_accept"])
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(fn=cmd_decisions)
    s = sp.add_parser("reevaluate")
    s.add_argument("trigger")
    s.set_defaults(fn=lambda a: post(f"/api/triggers/{a.trigger}/reevaluate", {}))
    s = sp.add_parser("dismiss")
    s.add_argument("decision")
    s.set_defaults(fn=lambda a: post(f"/api/decisions/{a.decision}/dismiss", {}))
    s = sp.add_parser("mode")
    s.add_argument("mode", choices=["off", "observe", "auto"])
    s.set_defaults(fn=lambda a: post("/api/mode", {"mode": a.mode, "origin": "cli"}))
    s = sp.add_parser("autoreply", help="钉钉自动回复：不带参数看状态和最近记录，auto 打开，off 关掉；"
                                        "exclude add|remove <群ID或群名> 管过滤群（这些群里 @我 不回）")
    s.add_argument("mode", nargs="?", choices=["auto", "off", "exclude"])
    s.add_argument("action", nargs="?", choices=["add", "remove"])
    s.add_argument("group", nargs="?", help="群 ID（cid 开头，优先）或群名")
    s.set_defaults(fn=lambda a: (post("/api/autoreply/exclude", {"action": a.action or "add", "group": a.group}) if a.mode == "exclude"
                                 else post("/api/autoreply", {"mode": a.mode, "origin": "cli"}) if a.mode else get("/api/autoreply")))
    for name in ("pause", "resume"):
        s = sp.add_parser(name)
        s.add_argument("key", nargs="?")
        s.add_argument("--reason", default="")
        s.set_defaults(fn=lambda a, n=name: post("/api/pause", {"paused": n == "pause", "key": a.key, "reason": a.reason}))
    sp.add_parser("reload").set_defaults(fn=lambda a: post("/api/reload", {"origin": "cli"}))
    s = sp.add_parser("fire")
    s.add_argument("type")
    s.set_defaults(fn=lambda a: post("/api/fire", {"type": a.type}))
    s = sp.add_parser("emit")
    s.add_argument("type")
    s.add_argument("--source", default="superops")
    s.add_argument("--subject")
    s.add_argument("--summary")
    s.add_argument("--data")
    s.add_argument("--dedup")
    s.set_defaults(fn=lambda a: post("/v1/events", {"source": a.source, "type": a.type, "subject": a.subject, "summary": a.summary,
                                                    "data": json.loads(a.data) if a.data else None, "dedup_key": a.dedup}))
    s = sp.add_parser("note")
    s.add_argument("text", nargs="+")
    s.set_defaults(fn=lambda a: post("/v1/events", {"source": "superops", "type": "decision.note", "summary": text(a)[:200], "data": {"text": text(a)}}))
    s = sp.add_parser("demand")
    s.add_argument("text", nargs="+")
    s.set_defaults(fn=lambda a: post("/v1/events", {"source": "superops", "type": "demand.manual", "subject": "demand:manual",
                                                    "summary": text(a)[:300], "data": {"text": text(a)}}))
    s = sp.add_parser("terms", help="监听意图词：add|enable|disable|remove <词> [--category pain|solution|competitor|brand]")
    s.add_argument("action", choices=["add", "enable", "disable", "remove"])
    s.add_argument("term")
    s.add_argument("--category", choices=["pain", "solution", "competitor", "brand", "persona"])
    s.set_defaults(fn=lambda a: post("/api/terms", {"action": a.action, "term": a.term, "category": a.category}))
    s = sp.add_parser("published")
    s.add_argument("pack_id")
    s.add_argument("url", nargs="?", help="作品链接；视频号没有链接可不填，数据采集时按标题对上作品")
    s.set_defaults(fn=lambda a: post("/v1/events", {"source": "superops", "type": "content.published.manual", "subject": f"pack:{a.pack_id}",
                                                    "summary": f"已发 {a.pack_id}", "data": {"pack_id": a.pack_id, **({"url": a.url} if a.url else {})}}))
    s = sp.add_parser("inbox")
    s.add_argument("-n", type=int, default=20)
    s.set_defaults(fn=cmd_inbox)
    s = sp.add_parser("agents")
    s.add_argument("action", nargs="?", choices=["reset", "usage", "open"])
    s.add_argument("agent", nargs="?")
    s.add_argument("value", nargs="?")
    s.set_defaults(fn=cmd_agents)
    s = sp.add_parser("digest")
    s.add_argument("--hours", type=float, default=24)
    s.set_defaults(fn=lambda a: out(call("GET", "/v1/digest", since=iso(now() - timedelta(hours=a.hours)))))
    s = sp.add_parser("preflight")
    s.add_argument("--refresh", action="store_true", help="重新检查（会无界面打开 Chrome 看各平台登录态）")
    s.set_defaults(fn=cmd_preflight)
    s = sp.add_parser("retention")
    s.add_argument("action", nargs="?", default="run")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_retention)
    s = sp.add_parser("metrics", help="collect [作品ID…] 立即采集（开工单，宿主执行）/ import <csv> / removed <作品ID…>")
    s.add_argument("action", choices=["collect", "import", "removed"])
    s.add_argument("args", nargs="*", help="collect / removed：作品 ID（posts.id），collect 不带就是最近 30 天全部；import：CSV 文件")
    s.set_defaults(fn=lambda a: post("/api/metrics/collect", {"post_ids": a.args, "origin": "cli"}) if a.action == "collect" else cmd_metrics(a))
    s = sp.add_parser("publish")
    s.add_argument("action", choices=["login", "qrlogin", "check", "try", "retry", "open"])
    s.add_argument("platform", nargs="?", default="all",
                   help="账号键：平台名就是主号（douyin / wechat_channels / xiaohongshu，只管账号的 kuaishou / bilibili / zhihu / wechat_oa），"
                        "同一平台后加的号是 douyin@2 这样；或 all（全部号）；try / retry 时是发布包 ID。"
                        "qrlogin：无界面打开登录页，二维码截图到 ~/popto-superops-data/publish-login/<账号键>.png（面板账号页显示），手机扫码；"
                        "retry：已批准但自动发布失败转了人工的，用原来的批准再发一次（批准须在 24 小时内）")
    s.add_argument("--live", action="store_true", help="try：真发布（默认只试运行）")
    s.add_argument("--draft", action="store_true", help="try：填好后存进平台草稿箱，不发布")
    s.add_argument("--headed", action="store_true")
    s.set_defaults(fn=cmd_publish)
    s = sp.add_parser("browse")
    s.add_argument("action", choices=["install", "login", "check", "sites"])
    s.add_argument("targets", nargs="*")
    s.add_argument("--proxy")
    s.set_defaults(fn=cmd_browse)
    s = sp.add_parser("panel")
    s.set_defaults(fn=lambda a: subprocess.run(["open", need_host(client()).base + "/"], check=False))
    s = sp.add_parser("launchd")
    s.add_argument("action", choices=["install"])
    s.add_argument("--load", action="store_true")
    s.add_argument("--print", action="store_true", help="只打印 plist，不写文件")
    s.set_defaults(fn=cmd_launchd)
    s = sp.add_parser("skills")
    s.add_argument("action", choices=["link"])
    s.set_defaults(fn=cmd_skills_link)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
