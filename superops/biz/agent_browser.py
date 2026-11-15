"""Agent 专用浏览器：playwright-cli + 本机 Chrome + 固定登录态目录（agents.yaml 的 agent_browser）。

Antigravity / Codex 自己没有浏览器工具，浏览器档位（tools=browser）的任务靠这里：

- 宿主在提示词里告诉 Agent 用 `ops-browser <命令>` 操作浏览器（`agent_note`），任务结束后关掉（`close`）；
- `ops-browser` 是 playwright-cli 的一层薄包装（`main`）：固定会话名和登录态目录，产物落在运行目录，
  拦掉读写 Cookie / 本地存储、上传文件、改网络请求这类命令；
- 登录由人完成：`superops browse login` 打开有界面的 Chrome，登录态存在 profile 目录，之后任务复用。

同一个登录态目录同一时间只能被一个 Chrome 打开，所以宿主对浏览器档位的任务串行执行。
浏览器由宿主在拉起 Agent 之前开好（`open_session`）：Codex 的沙箱里启动不了 Chrome，但能操作已经开着的。
playwright-cli 装在数据目录的 tools/ 下（`superops browse install`），不走 npx：沙箱里 npx 写不了 npm 缓存。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ..infra import paths, redact
from ..infra.timeutil import iso

SESSION = "ops-agent"
DEFAULT_CLI = ["npx", "-y", "@playwright/cli@0.1.24"]
PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")

# 不给 Agent 用的命令：登录态（Cookie / 存储）读写、上传、改请求、跑任意 Playwright 代码、动别的会话
BLOCKED_PREFIXES = ("cookie-", "state-", "localstorage-", "sessionstorage-", "request", "response-", "route", "unroute",
                    "network-state", "video-", "recording-", "tracing-", "webmcp-", "install")
BLOCKED = {"upload", "drop", "run-code", "delete-data", "attach", "detach", "close-all", "kill-all", "show", "pdf",
           "pause-at", "resume", "step-over"}
# open 的这些选项由包装层决定，Agent 传了也丢掉
OPEN_FIXED = ("--browser", "--profile", "--persistent", "--config", "--headed", "--device", "--mobile")
# eval 里出现这些就拦：读登录态
STORAGE_RE = re.compile(r"(?i)\bcookie(?:store)?\b|\b(?:local|session)storage\b|\bindexeddb\b")

# 需要登录才能搜索 / 提问的站点（superops browse login 默认逐个打开）
LOGIN_SITES = {
    "douyin": "https://www.douyin.com/", "xiaohongshu": "https://www.xiaohongshu.com/explore",
    "kuaishou": "https://www.kuaishou.com/", "bilibili": "https://www.bilibili.com/", "zhihu": "https://www.zhihu.com/",
    "doubao": "https://www.doubao.com/chat/", "deepseek": "https://chat.deepseek.com/",
    "tongyi": "https://www.tongyi.com/", "yuanbao": "https://yuanbao.tencent.com/chat",
    "kimi": "https://www.kimi.com/", "yiyan": "https://yiyan.baidu.com/",
}


def settings(cfg: dict) -> dict:
    ab = cfg.get("agent_browser") or {}
    profile = paths.expand(ab.get("profile")) or paths.ops_data() / "browser-profiles" / "agent-browse"
    cli = ab.get("cli")
    if isinstance(cli, str):
        cli = cli.split()
    if not cli:
        local = tools_dir() / "node_modules" / ".bin" / "playwright-cli"
        found = str(local) if local.exists() else shutil.which("playwright-cli")
        cli = [found] if found else list(DEFAULT_CLI)
    return {"enabled": ab.get("enabled", True), "profile": Path(profile), "cli": [os.path.expanduser(c) for c in cli],
            "headed": bool(ab.get("headed", True)), "session": ab.get("session") or SESSION}


def tools_dir() -> Path:
    return paths.ops_data() / "tools"


def install(proxy: str | None = None) -> int:
    """把 playwright-cli 装到数据目录的 tools/ 下（固定版本）。"""
    d = tools_dir()
    d.mkdir(parents=True, exist_ok=True)
    cmd = ["npm", "install", "--prefix", str(d), DEFAULT_CLI[-1]]
    if proxy:
        cmd += ["--proxy", proxy, "--https-proxy", proxy]
    return subprocess.run(cmd, check=False).returncode


def wrapper_path() -> str:
    """ops-browser 的绝对路径（和当前 Python 同一个虚拟环境）。"""
    p = Path(sys.executable).parent / "ops-browser"
    return str(p) if p.exists() else f"{sys.executable} -m superops.biz.agent_browser"


def blocked_reason(command: str, args: list[str] | None = None) -> str | None:
    if command in BLOCKED or command.startswith(BLOCKED_PREFIXES):
        return f"ops-browser 不提供 `{command}`：登录态读写、上传、改请求、跑任意代码这类命令在自动化里不开放。"
    if command == "eval" and args and STORAGE_RE.search(" ".join(args)):
        return "ops-browser 的 eval 不能读 Cookie / 本地存储（document.cookie、localStorage、sessionStorage、indexedDB）。"
    return None


def build_argv(args: list[str], s: dict, preopened: bool = False) -> list[str]:
    """Agent 传入的参数 → playwright-cli 的完整命令行。不合规时抛 ValueError。"""
    args = [a for a in args if not (a.startswith("-s=") or a.startswith("--session"))]
    command = next((a for a in args if not a.startswith("-")), None)
    if command is None:
        return [*s["cli"], *args]
    reason = blocked_reason(command, args[args.index(command) + 1:])
    if reason:
        raise ValueError(reason)
    out = [*s["cli"], f"-s={s['session']}"]
    if preopened and command == "open":
        # 浏览器已由宿主开好：open <网址> 当成跳转
        url = next((a for a in args[args.index("open") + 1:] if not a.startswith("-")), None)
        return out + (["goto", url] if url else ["tab-list"])
    if preopened and command == "close":
        return out + ["tab-list"]  # 不让 Agent 关：关了沙箱里重开不了，结束后由宿主关
    if command != "open":
        return out + args
    kept, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a.startswith(OPEN_FIXED):
            # “--profile 路径”这种分开写的值也要一起丢
            skip = "=" not in a and a in ("--browser", "--profile", "--config", "--device")
            continue
        kept.append(a)
    out += kept + ["--browser", "chrome", "--profile", str(s["profile"])]
    if s["headed"]:
        out.append("--headed")
    return out


def child_env(env: dict | None = None) -> dict:
    """浏览器直连国内站点：去掉 Agent 包装脚本带进来的海外代理。"""
    e = dict(os.environ if env is None else env)
    for k in PROXY_VARS:
        e.pop(k, None)
    return e


def run_dir_cwd(env: dict | None = None) -> str | None:
    d = (os.environ if env is None else env).get("OPS_RUN_DIR")
    return d if d and Path(d).is_dir() else None


def agent_note(cfg: dict) -> str:
    """写进提示词的浏览器用法说明。"""
    s = settings(cfg)
    w = wrapper_path()
    logged = "已有人工登录过的登录态" if is_initialized(s) else "**还没有人登录过**，需要登录才能看的站点按未登录处理"
    return (
        f"浏览器：本任务用 `{w} <命令>` 操作专用 Chrome（{logged}；目录由系统固定，不要自己另开浏览器，也不要用别的登录态）。"
        f"浏览器已经开好，直接用。常用：`{w} goto <网址>` 跳转；`snapshot` 取页面结构和元素编号；"
        "`find <文字>` 在页面里找；`click <编号>`、`fill <编号> <文字> --submit`、`type <文字>`、`press Enter` 操作；"
        "`eval \"document.body.innerText.slice(0, 4000)\"` 取正文；`screenshot --filename=<相对路径>.png` 截图；"
        "`tab-new <网址>` / `tab-list` / `tab-select <序号>` 管标签页。"
        "相对路径都相对运行目录，截图目录要先建好；页面快照存在运行目录的 `.playwright-cli/`。"
        "遇到登录墙、验证码、滑块：不要尝试登录或绕过，记下是哪个站点，写进 `ops_feedback`（area 写“浏览器登录态”），然后继续做其余部分。"
        "只读浏览：不发帖、不评论、不点赞、不关注、不私信。结束时不用关浏览器，系统会关。"
    )


LOGIN_MARK = ".ops-login.json"  # superops browse login 完成后写的标记（Chrome 自己建的目录不算登录过）


def is_initialized(s: dict) -> bool:
    return (s["profile"] / LOGIN_MARK).is_file()


def open_session(cfg: dict, env: dict | None = None, timeout: float = 90) -> str | None:
    """宿主在拉起 Agent 前打开专用浏览器。成功返回 None，失败返回原因。"""
    s = settings(cfg)
    s["profile"].mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(build_argv(["open", "about:blank"], s), env=child_env(env), cwd=run_dir_cwd(env),
                           capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)[:300]
    return None if r.returncode == 0 else (r.stderr or r.stdout or f"exit {r.returncode}")[-300:]


def close(cfg: dict, env: dict | None = None, timeout: float = 30) -> None:
    """任务结束后关掉专用浏览器（没开着也不报错）。"""
    s = settings(cfg)
    try:
        subprocess.run([*s["cli"], f"-s={s['session']}", "close"], env=child_env(env), cwd=run_dir_cwd(env),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def cli_version(cfg: dict, timeout: float = 120) -> str | None:
    s = settings(cfg)
    try:
        r = subprocess.run([*s["cli"], "--version"], capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip().splitlines()[-1] if r.returncode == 0 and r.stdout.strip() else None


def login(cfg: dict, urls: list[str]) -> int:
    """打开有界面的 Chrome 让人登录；人回车后关闭，登录态留在 profile 目录。"""
    s = dict(settings(cfg), headed=True)
    s["profile"].mkdir(parents=True, exist_ok=True)
    env = child_env()
    base = [*s["cli"], f"-s={s['session']}"]
    cwd = paths.ops_data() / "logs" / "browser-login"  # 页面快照别落在当前目录（可能是代码仓库）
    cwd.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(build_argv(["open", urls[0]], s), env=env, cwd=cwd, check=False)
    if r.returncode != 0:
        return r.returncode
    for u in urls[1:]:
        subprocess.run([*base, "tab-new", u], env=env, cwd=cwd, stdout=subprocess.DEVNULL, check=False)
    try:
        input(f"已在专用 Chrome 里打开 {len(urls)} 个站点。逐个登录完成后回到这里按回车（会关闭浏览器并保存登录态）……")
    except (EOFError, KeyboardInterrupt):
        pass
    subprocess.run([*base, "close"], env=env, cwd=cwd, stdout=subprocess.DEVNULL, check=False)
    (s["profile"] / LOGIN_MARK).write_text(json.dumps({"at": iso(), "sites": urls}, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    from ..infra import config as cfgmod

    args = list(sys.argv[1:] if argv is None else argv)
    cfg = cfgmod.load_agents_config()
    s = settings(cfg)
    if not s["enabled"]:
        print("ops-browser：agent_browser.enabled 为 false，专用浏览器已停用。", file=sys.stderr)
        return 2
    try:
        cmd = build_argv(args, s, preopened=os.environ.get("OPS_BROWSER_PREOPENED") == "1")
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    s["profile"].mkdir(parents=True, exist_ok=True)
    cwd = run_dir_cwd()
    started = time.time() - 2
    try:
        r = subprocess.run(cmd, env=child_env(), cwd=cwd, check=False, capture_output=True)
    except FileNotFoundError:
        print(f"ops-browser：找不到 {cmd[0]}（运行 superops doctor 看“专用浏览器”一行）。", file=sys.stderr)
        return 127
    # 页面会把带 Cookie 的请求头打到控制台：Agent 读到的输出和 .playwright-cli/ 里的快照、日志都先打码。
    # 不查钥匙串（secrets=()）：Agent 沙箱里访问钥匙串可能卡住，本系统的密钥也不会出现在网页里。
    if cwd:
        redact.scrub_tree(Path(cwd) / ".playwright-cli", since=started, mark=False, secrets=())
    for data, stream in ((r.stdout, sys.stdout), (r.stderr, sys.stderr)):
        if data:
            stream.write(redact.redact(data.decode("utf-8", "replace"), secrets=())[0])
            stream.flush()
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
