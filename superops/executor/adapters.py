"""各 Agent 的非交互调用。权限由规则的 tools 档位决定，不用人用的全权限包装脚本。

从 ops 的 opsd/agents.py 原样带过来，只加了一处：RunSpec.session（工单会话的续接）。"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from ..infra import paths
from ..infra.validate import extract_json, for_agent

# Claude 工具白名单（按档位）
_RO = ["Read", "Glob", "Grep"]
_WS = _RO + ["Write", "Edit", "Bash(python3:*)", "Bash(uv:*)", "Bash(ffmpeg:*)", "Bash(ffprobe:*)", "Bash(node:*)",
             "Bash(npx:*)", "Bash(ls:*)", "Bash(mkdir:*)", "Bash(cp:*)", "Bash(jq:*)"]
_DEV = _WS + ["Bash(git:*)", "Bash(npm:*)", "Bash(pnpm:*)", "Bash(go:*)", "Bash(make:*)", "Bash(pytest:*)",
              "Bash(python:*)"]          # 不给 glab / gh：推送和开 MR / PR 由宿主做
# 浏览器档位：网页搜索 / 抓取，加宿主给的 ops-browser（提示词里写的是 .venv/bin 下的绝对路径）。2026-11-12 竞品调研
# 只给了 _WS，Claude 在 -p 模式下这三样全被拒，一个官网都没查到。
_OPS_BROWSER = str(Path(sys.executable).parent / "ops-browser")
_BROWSER = _WS + ["WebSearch", "WebFetch", "Bash(ops-browser:*)", f"Bash({_OPS_BROWSER}:*)"]
CLAUDE_TOOLS = {"readonly": _RO, "workspace": _WS, "browser": _BROWSER, "dev": _DEV}
# 经 ~/claude.sh（bypassPermissions）启动时 allowedTools 不再起限制作用，只有禁用清单生效，所以禁用清单要写全。
_DENY_ALWAYS = ["Bash(security:*)", "Bash(sudo:*)", "Bash(ssh:*)", "Bash(scp:*)", "Bash(dws:*)", "Bash(lark-cli:*)",
                "Bash(launchctl:*)", "Bash(crontab:*)", "Bash(osascript:*)"]
CLAUDE_DENY = {"readonly": ["Bash", "WebFetch", "WebSearch", "Write", "Edit", "NotebookEdit"],
               "workspace": ["WebFetch", "WebSearch", "Bash(rm:*)", "Bash(curl:*)", "Bash(wget:*)", "Bash(git:*)",
                             "Bash(glab:*)", "Bash(gh:*)"] + _DENY_ALWAYS,
               "browser": ["Bash(rm:*)", "Bash(git:*)", "Bash(glab:*)", "Bash(gh:*)"] + _DENY_ALWAYS,
               # 开发档：推送和开 MR / PR 由宿主做；gh / glab 一律不给（gh auth token 能直接读出令牌）
               "dev": ["Bash(rm -rf:*)", "Bash(git push:*)", "Bash(glab:*)", "Bash(gh:*)"] + _DENY_ALWAYS}


@dataclass
class RunSpec:
    task_id: str
    run_dir: Path
    cwd: Path
    prompt: str
    schema: dict
    schema_path: Path
    tools: str
    effort: str | None
    model: str | None
    timeout: float
    agent_cfg: dict
    env: dict
    alive: Callable[[], bool] = lambda: True
    command: list[str] | None = None   # script 用
    extra_dirs: list[Path] = field(default_factory=list)  # 除运行目录外 Agent 需要访问 / 写入的目录（如主仓库 .git）
    session: str | None = None         # 工单的会话：适配器支持续接时（agents.<名>.resume: true）接着上一轮的上下文
    idle_timeout: float = 0            # >0：这么多秒里输出和运行目录都没有任何变化就提前结束（按超时算，交给下一个 Agent）
    network: bool = False              # 规则 run.network: true：要调外部接口（如 Popto 开放 API）。Codex 的 workspace 档默认断网，这时放开


@dataclass
class RunResult:
    ok: bool
    output: dict | None = None
    raw_text: str = ""
    session_id: str | None = None
    cost_usd: float | None = None
    error_text: str = ""
    timed_out: bool = False
    lease_lost: bool = False
    extra: dict = field(default_factory=dict)


IDLE_CHECK_EVERY = 15.0


def last_activity(paths: list[Path]) -> float:
    """这些文件 / 目录（递归）里最新的修改时间。"""
    newest = 0.0
    for root in paths:
        try:
            if root.is_file():
                newest = max(newest, root.stat().st_mtime)
            elif root.is_dir():
                newest = max(newest, root.stat().st_mtime, *(f.stat().st_mtime for f in root.rglob("*")))
        except (OSError, ValueError):
            continue
    return newest


def run_process(cmd: list[str], *, cwd: Path, env: dict, timeout: float, log_path: Path,
                stdin: str | None = None, alive: Callable[[], bool] = lambda: True,
                idle_timeout: float = 0, watch: Path | None = None) -> tuple[int, str, str, bool, bool]:
    """跑子进程（独立进程组），超时、长时间没有动静或租约丢失时整组杀掉。返回 (rc, stdout, stderr, timed_out, lease_lost)。
    idle_timeout > 0 时每 15 秒看一次：标准输出 / 错误和 watch 目录（运行目录，含浏览器的页面快照）里有没有新变化，
    连续 idle_timeout 秒都没有就当超时结束（2026-11-12 Antigravity 在浏览器补位监听里卡住 20 多分钟、CPU 为 0）。"""
    out_p, err_p = log_path.with_suffix(".stdout"), log_path.with_suffix(".stderr")
    with out_p.open("w") as fo, err_p.open("w") as fe:
        fe.write(f"$ {' '.join(cmd[:12])}{' …' if len(cmd) > 12 else ''}\n")
        fe.flush()
        proc = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                stdout=fo, stderr=fe, start_new_session=True, text=True)
        if stdin is not None:
            try:
                proc.stdin.write(stdin)
                proc.stdin.close()
            except BrokenPipeError:
                pass
        start = time.monotonic()
        timed_out = lost = False
        watched = [out_p, err_p, *([watch] if watch else [])]
        newest, still_since = (last_activity(watched) if idle_timeout else 0.0), start
        next_check = start + IDLE_CHECK_EVERY
        while proc.poll() is None:
            if time.monotonic() - start > timeout:
                timed_out = True
            elif not alive():
                lost = True
            elif idle_timeout and time.monotonic() >= next_check:
                next_check = time.monotonic() + IDLE_CHECK_EVERY
                cur = last_activity(watched)
                if cur > newest:                      # 有新动静：从现在重新计时
                    newest, still_since = cur, time.monotonic()
                elif time.monotonic() - still_since > idle_timeout:
                    timed_out = True
                    fe.write(f"\n[superops] {int(idle_timeout // 60)} 分钟没有任何动作（输出、运行目录、浏览器快照都没有变化），提前结束\n")
                    fe.flush()
            if timed_out or lost:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    proc.wait(10)
                except Exception:  # noqa: BLE001
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except Exception:  # noqa: BLE001
                        pass
                break
            time.sleep(0.5)
        rc = proc.wait()
    return rc, out_p.read_text(errors="replace"), err_p.read_text(errors="replace"), timed_out, lost


def _report(*candidates) -> dict | None:
    """在几处来源里挑出报告：先要“像报告”的（有 state，或旧公共输出的 status + summary），都不像才退回第一个 dict。
    2026-11-12：Codex 按 Skill 在工作目录写了一个只有结构化字段的 output.json，宿主优先读了它，把最后一条消息里
    完整的报告丢了，判成“缺 state / summary”。"""
    dicts = [c for c in candidates if isinstance(c, dict)]
    for c in dicts:
        if "state" in c or {"status", "summary"} <= set(c):
            return c
    return dicts[0] if dicts else None


def _read_output_file(run_dir: Path) -> dict | None:
    p = run_dir / "output.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
    return None


def _bin(cfg: dict) -> str:
    return str(Path(os.path.expanduser(cfg["bin"])))


# ---- 找命令：本机装没装、该调哪个文件，看命令本身（claude / codex / agy），不看包装脚本在不在

COMMANDS = {"claude": ("claude",), "codex": ("codex",), "antigravity": ("agy", "antigravity")}
COMMON_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "~/.npm-global/bin", "~/.bun/bin",
               "~/.antigravity/antigravity/bin")


def _is_exe(p: str | None) -> bool:
    if not p:
        return False
    q = Path(os.path.expanduser(p))
    return q.is_file() and os.access(q, os.X_OK)


def _version_key(p: Path) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", p.parent.name))


def command_dirs(search_path: str = "") -> list[str]:
    """找命令的目录，按先后：agents.yaml 的 path、宿主进程的 PATH、常见安装目录、nvm 各版本（新的在前）。
    launchd 拉起的宿主 PATH 很短，nvm / 安装器装的命令全靠后两类找到。"""
    nvm = sorted(Path("~/.nvm/versions/node").expanduser().glob("*/bin"), key=_version_key, reverse=True)
    out: list[str] = []
    for d in [*search_path.split(os.pathsep), *os.environ.get("PATH", "").split(os.pathsep), *COMMON_DIRS, *map(str, nvm)]:
        d = os.path.expanduser(d) if d else ""
        if d and d not in out:
            out.append(d)
    return out


def find_command(cfg: dict, search_path: str = "") -> str | None:
    """Agent 的命令本体在哪：agents.yaml 里 agents.<名>.command（命令名、命令名列表或路径）优先，
    没写就按类型（claude → claude，codex → codex，antigravity → agy / antigravity）。找不到返回 None。"""
    names = cfg.get("command") or COMMANDS.get(cfg.get("type", ""), ())
    dirs = None
    for name in [names] if isinstance(names, str) else names:
        if os.sep in name or name.startswith("~"):
            if _is_exe(name):
                return os.path.expanduser(name)
            continue
        dirs = dirs if dirs is not None else os.pathsep.join(command_dirs(search_path))
        found = shutil.which(name, path=dirs)
        if found:
            return found
    return None


def direct_bin(cfg: dict, search_path: str = "") -> str | None:
    """不经包装脚本的命令本体：strict_bin 在就用它，否则按命令名找。"""
    return os.path.expanduser(cfg["strict_bin"]) if _is_exe(cfg.get("strict_bin")) else find_command(cfg, search_path)


def uses_wrapper(cfg: dict) -> bool:
    """bin 配的包装脚本（~/codex.sh 这类，自己设代理和权限参数）在本机存在。"""
    b = cfg.get("bin")
    return bool(b) and Path(os.path.expanduser(b)).is_file()


def with_command_dir(env: dict, cmd: str | None) -> dict:
    """命令所在目录放到 PATH 最前：nvm 装的 claude / codex 是 `#!/usr/bin/env node` 脚本，node 就在同一目录；
    包装脚本里 `exec codex` 也靠它找到命令。"""
    if not cmd:
        return env
    d = str(Path(cmd).parent)
    parts = env.get("PATH", "").split(os.pathsep)
    if d in parts:
        return env
    return {**env, "PATH": os.pathsep.join([d, *[p for p in parts if p]])}


_PROXY_RE = re.compile(r'^\s*(?:export\s+)?PROXY=["\']?(https?://[^"\'\s]+)', re.M)


def wrapper_proxy(bin_path: str) -> str | None:
    """包装脚本（~/agy.sh 等）里 PROXY= 的值；不是脚本或没写代理时返回 None。"""
    p = Path(os.path.expanduser(bin_path))
    if p.suffix != ".sh" or not p.is_file():
        return None
    m = _PROXY_RE.search(p.read_text(errors="replace"))
    return m.group(1) if m else None


def with_proxy(env: dict, proxy: str | None) -> dict:
    if not proxy:
        return env
    env = dict(env)
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        env[k] = proxy
    return env


class Adapter:
    def run(self, spec: RunSpec, log_path: Path) -> RunResult:
        raise NotImplementedError

    @staticmethod
    def strict(spec: RunSpec) -> bool:
        """该档位是否绕开包装脚本直调 strict_bin（包装脚本的全权限参数会压过档位限制时）。"""
        c = spec.agent_cfg
        return bool(c.get("strict_bin")) and spec.tools in (c.get("strict_tiers") or [])

    def resolve_bin(self, spec: RunSpec) -> str:
        """这一轮调哪个文件：要直调的档位用命令本体；否则包装脚本在就用包装脚本，不在就直接调命令。
        都找不到时退回配置里写的路径（池子按 available() 判为没装，正常不会走到这里）。"""
        c, sp = spec.agent_cfg, spec.env.get("PATH", "")
        if self.strict(spec):
            return direct_bin(c, sp) or os.path.expanduser(c["strict_bin"])
        if uses_wrapper(c):
            return _bin(c)
        return find_command(c, sp) or (_bin(c) if c.get("bin") else (COMMANDS.get(c.get("type", "")) or ("?",))[0])

    def resolve_env(self, spec: RunSpec) -> dict:
        c = spec.agent_cfg
        env = spec.env
        if self.strict(spec) or not uses_wrapper(c):   # 包装脚本自己设代理；直调命令时代理从配置或包装脚本里读出来自己设
            env = with_proxy(env, c.get("proxy") or wrapper_proxy(c.get("bin", "")))
        return with_command_dir(env, find_command(c, env.get("PATH", "")) or direct_bin(c, env.get("PATH", "")))

    def available(self, cfg: dict, search_path: str = "") -> bool:
        """本机装没装：命令本体找得到就算装了（包装脚本在不在不影响）；bin 直接指向命令本体（不是 .sh）且存在也算。"""
        b = cfg.get("bin")
        return bool(direct_bin(cfg, search_path) or (_is_exe(b) and not str(b).endswith(".sh")))


class ClaudeAdapter(Adapter):
    def build_cmd(self, spec: RunSpec) -> list[str]:
        c = spec.agent_cfg
        cmd = [self.resolve_bin(spec), "-p", "--output-format", "json",
               "--permission-mode", "default" if spec.tools == "readonly" else "acceptEdits",
               "--allowedTools", ",".join(CLAUDE_TOOLS.get(spec.tools, _RO)),
               "--disallowedTools", ",".join(CLAUDE_DENY.get(spec.tools, [])),
               "--add-dir", str(spec.run_dir)]
        for d in spec.extra_dirs:
            cmd += ["--add-dir", str(d)]
        model = spec.model or c.get("default_model")
        if model:
            cmd += ["--model", model]
        if spec.effort:
            cmd += ["--effort", spec.effort]
        if c.get("structured_output", True):
            cmd += ["--json-schema", json.dumps(for_agent(spec.schema), ensure_ascii=False)]
        if spec.session and c.get("resume"):
            cmd += ["--resume", spec.session]
        return cmd

    def run(self, spec, log_path):
        rc, out, err, to, lost = run_process(self.build_cmd(spec), cwd=spec.cwd, env=self.resolve_env(spec),
                                             timeout=spec.timeout, log_path=log_path, stdin=spec.prompt, alive=spec.alive, idle_timeout=spec.idle_timeout, watch=spec.run_dir)
        res = RunResult(ok=False, raw_text=out, timed_out=to, lease_lost=lost)
        data = extract_json(out) if out.strip() else None
        if isinstance(data, dict):
            res.session_id = data.get("session_id")
            res.cost_usd = data.get("total_cost_usd")
            if data.get("is_error") or rc != 0:
                res.error_text = f"{data.get('subtype')}: {data.get('result')}\n{err[-1500:]}"
                return res
            output = data.get("structured_output")
            if not isinstance(output, dict):
                output = _report(extract_json(str(data.get("result") or "")), _read_output_file(spec.run_dir))
            res.output = output if isinstance(output, dict) else None
            res.ok = True
            return res
        res.error_text = (err[-2000:] or out[-2000:]) or f"exit {rc}"
        return res


class CodexAdapter(Adapter):
    """续接（agents.codex.resume: true）：同一张工单的下一轮用 `codex … exec resume <会话 ID>` 接着上一轮的会话。
    续接轮必须直调 strict_bin（Codex 本体）：包装脚本 ~/codex.sh 固定带 `-s danger-full-access`，`exec resume` 不收 `-s`，
    用 `-c sandbox_mode=…` 也压不过它（2026-11-12 实测：只读档的续接轮照样写出了文件）。直调时沙箱、工作目录、
    可写目录放在顶层参数里，代理从包装脚本里读出来自己设（实测只读档被拦住、会话记忆还在）。"""

    @staticmethod
    def resuming(spec: RunSpec) -> bool:
        c = spec.agent_cfg
        return bool(spec.session and c.get("resume") and (c.get("strict_bin") or direct_bin(c, spec.env.get("PATH", ""))))

    def resolve_env(self, spec: RunSpec) -> dict:
        if self.resuming(spec):
            c = spec.agent_cfg
            env = with_proxy({**spec.env, "BROWSER": "/bin/false"}, c.get("proxy") or wrapper_proxy(c.get("bin", "")))
            return with_command_dir(env, direct_bin(c, env.get("PATH", "")))
        return super().resolve_env(spec)

    def build_cmd(self, spec: RunSpec, last_msg: Path) -> list[str]:
        c = spec.agent_cfg
        sandbox = "read-only" if spec.tools == "readonly" else "workspace-write"
        dirs = [] if sandbox == "read-only" else [str(spec.run_dir), *[str(d) for d in spec.extra_dirs]]
        if self.resuming(spec):
            cmd = [direct_bin(c, spec.env.get("PATH", "")) or os.path.expanduser(c["strict_bin"]), "-a", "never", "-s", sandbox, "-C", str(spec.cwd)]
            for d in dirs:
                cmd += ["--add-dir", d]
            cmd += ["exec", "resume", spec.session, "--skip-git-repo-check", "--json", "-o", str(last_msg)]
        else:
            cmd = [self.resolve_bin(spec), "exec", "--skip-git-repo-check", "-C", str(spec.cwd), "-s", sandbox, "--json",
                   "-o", str(last_msg)]
            for d in dirs:
                cmd += ["--add-dir", d]
        if sandbox == "workspace-write" and (spec.tools == "browser" or spec.network):
            # 浏览器档：操作 opsd 预先开好的专用浏览器要连本机套接字（Chrome 本身不在沙箱里启动）；
            # run.network: true 的规则要调外部接口。只读档不放开（处理不可信内容时不能往外发）
            cmd += ["-c", "sandbox_workspace_write.network_access=true"]
        if spec.effort:      # Codex 0.166 起支持 low / medium / high / xhigh / max
            cmd += ["-c", f"model_reasoning_effort={spec.effort}"]
        if spec.model or c.get("default_model"):
            cmd += ["-m", spec.model or c["default_model"]]
        if c.get("structured_output"):
            cmd += ["--output-schema", str(spec.schema_path)]
        cmd.append("-")  # 从 stdin 读提示词
        return cmd

    def run(self, spec, log_path):
        res = self._run(spec, log_path)
        if self.resuming(spec) and not res.ok and not res.timed_out and not res.lease_lost:
            # 续不上（会话文件没了、ID 不认等）：新开一个会话再跑一次，提示词里“到目前为止”的摘要兜底
            res = self._run(replace(spec, session=None), log_path.with_name(log_path.stem + "-fresh.log"))
        return res

    def _run(self, spec, log_path):
        last = spec.run_dir / f"{log_path.stem}.last.txt"
        rc, out, err, to, lost = run_process(self.build_cmd(spec, last), cwd=spec.cwd, env=self.resolve_env(spec),
                                             timeout=spec.timeout, log_path=log_path, stdin=spec.prompt, alive=spec.alive, idle_timeout=spec.idle_timeout, watch=spec.run_dir)
        res = RunResult(ok=False, raw_text=out, timed_out=to, lease_lost=lost)
        errors, completed, failed = [], False, False
        for line in out.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "thread.started":
                res.session_id = ev.get("thread_id")
            elif t == "turn.completed":
                completed = True
            elif t == "turn.failed":
                failed = True
                errors.append(json.dumps(ev, ensure_ascii=False)[:500])
            elif t == "error":  # 含 "Reconnecting... 2/5" 这类过程提示，只有最终没完成时才算失败
                errors.append(json.dumps(ev, ensure_ascii=False)[:500])
        if rc != 0 or failed or not completed:
            # 只用 Codex 自己报的错误事件归类；stderr 里常有无关噪音（如 MCP 服务连不上），没有错误事件时才看它
            res.error_text = "\n".join(errors[-6:]) if errors else err[-1500:]
            return res
        text = last.read_text(errors="replace") if last.exists() else ""
        output = _report(extract_json(text), _read_output_file(spec.run_dir))
        res.output = output if isinstance(output, dict) else None
        res.ok = True
        return res


def readonly_allow_rules(spec: RunSpec) -> list[str]:
    """运行目录、附加目录，以及运行目录里软链接指向的真实目录（agy 按真实路径判权限）。"""
    dirs: list[Path] = [spec.run_dir.resolve(), *[Path(d).resolve() for d in spec.extra_dirs]]
    try:
        for p in spec.run_dir.rglob("*"):
            if p.is_symlink():
                t = p.resolve()
                dirs.append(t if t.is_dir() else t.parent)
    except OSError:
        pass
    seen, rules = set(), []
    for d in dirs:
        if str(d) in seen:
            continue
        seen.add(str(d))
        rules += [f"read_file({d}/**)", f"list_dir({d}/**)", f"list_dir({d})"]
    return rules


class AntigravityAdapter(Adapter):
    def build_cmd(self, spec: RunSpec) -> list[str]:
        c = spec.agent_cfg
        b = self.resolve_bin(spec)
        cmd = [b, "-p", spec.prompt, "--output-format", "json", "--add-dir", str(spec.run_dir),
               *[x for d in spec.extra_dirs for x in ("--add-dir", str(d))],
               "--print-timeout", f"{int(spec.timeout)}s"]
        if spec.effort:
            cmd += ["--effort", spec.effort]
        if spec.model or c.get("default_model"):
            cmd += ["--model", spec.model or c["default_model"]]
        if spec.tools == "readonly":
            cmd.append("--sandbox")
            # 无人值守时 agy 连读文件都会被拒（且一被拒就整次不输出）：只放行读 / 列目录，写仍然拦住
            cmd += ["--settings", json.dumps({"permissions": {"allow": readonly_allow_rules(spec)}}, ensure_ascii=False)]
        elif not b.endswith(".sh"):  # 包装脚本自己已带
            cmd.append("--dangerously-skip-permissions")
        if c.get("structured_output", True):
            cmd += ["--json-schema", str(spec.schema_path)]
        return cmd

    def run(self, spec, log_path):
        rc, out, err, to, lost = run_process(self.build_cmd(spec), cwd=spec.cwd, env=self.resolve_env(spec),
                                             timeout=spec.timeout + 60, log_path=log_path, alive=spec.alive, idle_timeout=spec.idle_timeout, watch=spec.run_dir)
        res = RunResult(ok=False, raw_text=out, timed_out=to, lease_lost=lost)
        data = extract_json(out)
        if rc != 0:
            res.error_text = (err[-2000:] or out[-2000:]) or f"exit {rc}"
            return res
        output = None
        if isinstance(data, dict):
            res.session_id = data.get("conversation_id") or data.get("conversationId") or data.get("session_id")
            if {"status", "summary"} <= set(data) or {"state", "summary"} <= set(data):   # 旧公共输出 / 工单报告
                output = data
            else:
                for k in ("structured_output", "result", "response", "output", "text"):
                    v = data.get(k)
                    if isinstance(v, dict):
                        output = v
                        break
                    if isinstance(v, str):
                        output = extract_json(v)
                        if output:
                            break
        output = _report(output, _read_output_file(spec.run_dir))
        res.output = output if isinstance(output, dict) else None
        res.ok = True
        return res


class InboxAdapter(Adapter):
    """WorkBuddy：写收件箱，等发件箱（只承接不紧急的任务）。"""

    def available(self, cfg, search_path=""):
        return True

    def run(self, spec, log_path):
        c = spec.agent_cfg
        inbox = Path(os.path.expanduser(c["inbox"]))
        outbox = Path(os.path.expanduser(c["outbox"]))
        inbox.mkdir(parents=True, exist_ok=True)
        outbox.mkdir(parents=True, exist_ok=True)
        out_file = outbox / f"{spec.task_id}.json"
        msg = (f"{spec.prompt}\n\n---\n## 交付方式（WorkBuddy 收件箱任务）\n"
               f"处理完后把结果 JSON 写到：`{out_file}`（只写 JSON 对象）。处理完把本文件改名为 `{spec.task_id}.done.md`。\n")
        (inbox / f"{spec.task_id}.md").write_text(msg, encoding="utf-8")
        start = time.monotonic()
        while time.monotonic() - start < spec.timeout:
            if out_file.exists():
                time.sleep(1)
                out = extract_json(out_file.read_text(encoding="utf-8", errors="replace"))
                return RunResult(ok=isinstance(out, dict), output=out if isinstance(out, dict) else None,
                                 error_text="" if isinstance(out, dict) else "发件箱文件不是 JSON")
            if not spec.alive():
                return RunResult(ok=False, lease_lost=True, error_text="租约丢失")
            time.sleep(10)
        return RunResult(ok=False, timed_out=True, error_text="WorkBuddy 收件箱任务超时未处理")


class ScriptAdapter(Adapter):
    """确定性脚本：stdout 最后一个 JSON 对象就是 output（同样要符合公共 Schema）。"""

    def available(self, cfg, search_path=""):
        return True

    def run(self, spec, log_path):
        cmd = [sys.executable if x == "{python}" else x.replace("{run_dir}", str(spec.run_dir)) for x in spec.command or []]
        env = dict(spec.env)
        pkg_root = Path(__file__).resolve().parent.parent.parent
        env["PYTHONPATH"] = str(pkg_root) + os.pathsep + env.get("PYTHONPATH", "")
        rc, out, err, to, lost = run_process(cmd, cwd=paths.ops_home(), env=env, timeout=spec.timeout,
                                             log_path=log_path, alive=spec.alive, idle_timeout=spec.idle_timeout, watch=spec.run_dir)
        res = RunResult(ok=False, raw_text=out, timed_out=to, lease_lost=lost)
        if rc != 0:
            res.error_text = err[-2000:] or f"exit {rc}"
            return res
        output = None
        for line in reversed(out.strip().splitlines()):
            output = extract_json(line)
            if isinstance(output, dict):
                break
        output = _report(output, _read_output_file(spec.run_dir), extract_json(out))
        res.output = output if isinstance(output, dict) else None
        res.ok = True
        return res


class CommandAdapter(ScriptAdapter):
    """测试 / 自定义 Agent：bin + args，提示词从 stdin 进。"""

    def available(self, cfg, search_path=""):
        return True

    def run(self, spec, log_path):
        c = spec.agent_cfg
        cmd = [os.path.expanduser(x) for x in ([c["bin"]] + list(c.get("args") or []))]
        rc, out, err, to, lost = run_process(cmd, cwd=spec.cwd, env=spec.env, timeout=spec.timeout,
                                             log_path=log_path, stdin=spec.prompt, alive=spec.alive, idle_timeout=spec.idle_timeout, watch=spec.run_dir)
        res = RunResult(ok=False, raw_text=out, timed_out=to, lease_lost=lost)
        if rc != 0:
            res.error_text = (err[-2000:] or out[-2000:]) or f"exit {rc}"
            return res
        output = _report(_read_output_file(spec.run_dir), extract_json(out))
        res.output = output if isinstance(output, dict) else None
        res.ok = True
        return res


ADAPTERS: dict[str, Adapter] = {
    "claude": ClaudeAdapter(), "codex": CodexAdapter(), "antigravity": AntigravityAdapter(),
    "inbox": InboxAdapter(), "script": ScriptAdapter(), "command": CommandAdapter(),
}


def adapter_for(agent_cfg: dict) -> Adapter:
    return ADAPTERS[agent_cfg.get("type", "command")]
