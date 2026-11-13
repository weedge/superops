"""启动检查：要提前登录、要提前放好密钥的东西，服务一启动就查一遍并主动告诉人。

不这样做的话，这些缺口要等到发布、采集、审查真的跑起来才暴露：晚上 8 点的发布转了人工，才发现抖音没登录。
所以原则是“提前说”：启动后（以及每天 16:30 的登录检查）把没准备好的列成一条消息发到 home 渠道，
写清缺什么、影响哪些规则、一条命令怎么补；同样的缺口一天只说一次。全部就绪时不出声。

    kind    检查什么                                   怎么补
    login   创作者后台登录态（抖音 / 视频号 / 小红书）    superops publish login <平台>
    login   Agent 专用浏览器登录态（监听、GEO）          superops browse login
    key     钥匙串密钥（TikHub、GitLab）                security add-generic-password -s popto-ops/<名> -a popto-ops -w
    tool    本机命令（Agent CLI、dws、ffmpeg）           安装或登录对应的命令行

只检查“有规则用得到”的：对应规则都停用了，或平台的发布模式是 off、没配 CI 项目，就不查也不提醒。
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from ..biz import agent_browser
from ..biz.scriptkit import keychain
from ..core.model import now_iso
from ..core.reasons import key

log = logging.getLogger("superops.preflight")

# 每样东西被哪些规则用到（规则 id）。规则停用了就不算。
PUBLISH_RULES = ("publish-run", "collect-metrics", "collect-metrics-now", "lead-comment")          # 发布；指标采集读创作者后台，复用发布的登录态
BROWSER_RULES = ("social-listening", "geo-check")
KEY_RULES = {
    "tikhub": ("collect-comments", "collect-metrics", "collect-metrics-now", "listening-collect", "competitor-collect", "competitor-research-collect"),      # 评论采集；抖音指标读不到后台时的兜底
    "gitlab": ("ci-poll", "mr-review", "review-post", "merge-gate", "merge", "prd-writing", "author-fix"),
}
KEY_TITLES = {"tikhub": "TikHub 密钥", "gitlab": "GitLab 令牌（机器人账号，api 权限）"}


def _used_by(settings: dict, rule_ids) -> list[str]:
    """这些规则里启用着的，返回它们的名字。"""
    by_id = {r["id"]: r for r in settings["policy"]["rules"]}
    return [by_id[i]["name"] for i in rule_ids if i in by_id and by_id[i]["enabled"]]


def static_checks(cfg: dict, settings: dict) -> list[dict]:
    """不用开浏览器、立刻能查完的：密钥、本机命令、专用浏览器是否登录过。"""
    items: list[dict] = []
    for name, rules in KEY_RULES.items():
        used = _used_by(settings, rules)
        if name == "gitlab" and not ((cfg.get("ci") or {}).get("projects")):
            used = []        # 没接入任何仓库：CI 这几条规则不会被触发
        if not used:
            continue
        ok = bool(keychain(name))
        items.append({"id": f"key:{name}", "kind": "key", "title": KEY_TITLES[name], "ok": ok, "rules": used,
                      "detail": "钥匙串里已有" if ok else "钥匙串里没有",
                      "fix": f"security add-generic-password -s popto-ops/{name} -a popto-ops -w <密钥>"})
    used = _used_by(settings, BROWSER_RULES)
    browser = agent_browser.settings(cfg)
    if used and browser["enabled"]:
        ok = agent_browser.is_initialized(browser)
        items.append({"id": "login:agent-browser", "kind": "login", "title": "Agent 专用浏览器登录态", "ok": ok, "rules": used,
                      "detail": str(browser["profile"]) if ok else "还没登录过任何站点",
                      "fix": "superops browse install && superops browse login"})
    roles = {r["run"].get("agent") for r in settings["policy"]["rules"] if r["enabled"] and r["run"].get("executor") == "agent"}
    for name, agent in (cfg.get("agents") or {}).items():
        binary = agent.get("bin")
        if not binary or name not in roles:
            continue
        ok = Path(os.path.expanduser(binary)).exists()
        used = [r["name"] for r in settings["policy"]["rules"] if r["enabled"] and r["run"].get("agent") == name]
        items.append({"id": f"tool:{name}", "kind": "tool", "title": f"Agent {name}", "ok": ok, "rules": used[:6],
                      "detail": binary if ok else f"找不到 {binary}", "fix": f"安装并登录 {name} 的命令行，确认 {binary} 可执行"})
    if (settings["sources"].get("dingtalk") or {}).get("enabled"):
        ok = bool(shutil.which("dws"))
        items.append({"id": "tool:dws", "kind": "tool", "title": "钉钉命令行 dws", "ok": ok, "rules": ["钉钉来源（收消息、发卡片）"],
                      "detail": shutil.which("dws") or "PATH 里没有 dws", "fix": "安装 dws 并登录（dws auth login）"})
    if (settings["sources"].get("lark") or {}).get("enabled"):
        from ..biz.notifier import lark
        status = lark.auth_status() if shutil.which("lark-cli") else {"ok": False, "detail": "PATH 里没有 lark-cli"}
        items.append({"id": "tool:lark-cli", "kind": "tool", "title": "飞书命令行 lark-cli（本人身份）", "ok": bool(status.get("ok")),
                      "rules": ["飞书来源（收消息、发卡片、自动回复）"],
                      "detail": f"{status.get('user') or ''} {status.get('detail') or ''}".strip(),
                      "fix": "lark-cli auth login（用户登录 7 天要续一次）"})
    return items


def login_checks(cfg: dict, settings: dict, check=None) -> list[dict]:
    """创作者后台的登录态：要无界面打开一次 Chrome，每个平台十来秒。check(platform) -> (是否登录, 网址)，测试时可替换。"""
    from ..biz.executors import publishers as pubs
    used = _used_by(settings, PUBLISH_RULES)
    items: list[dict] = []
    for platform, pcfg in (cfg.get("publishers") or {}).items():
        mode = str((pcfg or {}).get("mode") or "off")
        if platform not in pubs.PLATFORMS or mode == "off" or not used:
            continue
        label = pubs.PLATFORMS[platform]["label"]
        try:
            ok, where = (check or pubs.check_login)(platform)
            detail = "已登录" if ok else "未登录"
        except Exception as error:  # noqa: BLE001  浏览器起不来、目录被占用等：当作没查成，照实说
            ok, detail = False, f"没查成：{str(error)[:120]}"
        items.append({"id": f"login:{platform}", "kind": "login", "title": f"{label}创作者后台登录态", "ok": ok, "rules": used,
                      "detail": f"{detail}（发布模式 {mode}）", "fix": f"superops publish login {platform}"})
    return items


def run(cfg: dict, settings: dict, *, logins: bool = True, check=None) -> dict:
    items = static_checks(cfg, settings) + (login_checks(cfg, settings, check) if logins else [])
    return {"at": now_iso(), "logins_checked": logins, "items": items, "missing": [i for i in items if not i["ok"]]}


def render(result: dict) -> str:
    """没准备好的写成一条给人看的消息；全部就绪返回空串（不出声）。"""
    missing = result["missing"]
    if not missing:
        return ""
    lines = [f"**启动检查：{len(missing)} 项还没准备好**", "", "这些不补上，相关的事到时候会失败或转人工："]
    labels = {"login": "要登录", "key": "缺密钥", "tool": "缺命令"}
    for item in missing:
        lines += ["", f"**{labels.get(item['kind'], item['kind'])} · {item['title']}**（{item['detail']}）",
                  f"- 影响：{'、'.join(item['rules'])}", f"- 怎么补：`{item['fix']}`"]
    lines += ["", "补完后运行 `superops preflight --refresh` 复查。"]
    return "\n".join(lines)


def fingerprint(result: dict) -> str:
    """缺口的指纹：同样的缺口当天只提醒一次。"""
    return key("preflight", now_iso()[:10], *sorted(i["id"] for i in result["missing"]))
