"""打码：Cookie、Authorization、各类 token / 密钥、手机号不进日志、不进输出（08 §5.2、AGENTS.md 第 7 条）。

- `redact(text)`：返回 (打码后的文字, 打码处数)；
- `redact_obj(obj)`：JSON 结构里的字符串逐个打码；
- `scrub_tree(目录)`：目录里的文本文件就地打码（跳过图片视频、软链接、超大文件），完成后写 `.scrubbed` 标记。

用在三处：宿主每个任务跑完后扫运行目录、回传前的结果；ops-browser 每条命令的输出和
`.playwright-cli/` 里的页面快照、控制台日志（页面自己会把带 Cookie 的请求头打到控制台）；
数据保留清理每天再补扫一遍没打过码的运行目录（宿主中途被杀时来不及扫）。
"""
from __future__ import annotations

import os
import re
import stat
import time
from pathlib import Path

MASK = "<已打码>"
PHONE_MASK = "<手机号已打码>"
MARKER = ".scrubbed"
MAX_BYTES = 50 * 1024 * 1024
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".heic", ".mp4", ".mov", ".m4v", ".webm",
            ".mkv", ".avi", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".pdf", ".zip", ".gz", ".tgz", ".xz",
            ".7z", ".db", ".sqlite", ".wasm", ".woff", ".woff2", ".ttf", ".otf", ".pyc", ".bin"}
_NOT_MASKED = r"(?!<已打码>)"

# (正则, 替换)。替换里用 \g<k> 保留键名，只把值换掉。
_RULES: list[tuple[re.Pattern, str]] = [
    # 请求头 / JSON / 代码里的 Cookie、Authorization 等：值整段打码（到引号或行尾为止）
    (re.compile(r"(?i)(?P<k>\b(?:set-cookie|cookie|proxy-authorization|authorization|x-api-key|x-auth-token"
                r"|x-access-token|x-csrf-token|x-tt-token)\b[\"']?\s*[:=]\s*\[?\s*[\"']?)" + _NOT_MASKED
                + r"[^\s\"'\r\n][^\"'\r\n]{3,}"), r"\g<k>" + MASK),
    # Playwright / Chrome 导出的 Cookie 对象：{"name": "…", "value": "…", "domain": …}
    (re.compile(r"(?P<k>\"name\"\s*:\s*\"[^\"]{1,80}\"\s*,\s*\"value\"\s*:\s*\")" + _NOT_MASKED
                + r"[^\"]{4,}(?=\"\s*,\s*\"domain\")"), r"\g<k>" + MASK),
    # 常见的会话 Cookie 名、token / 密钥字段（键值对、URL 参数、JSON 都算）。
    # 不含单独的 token：搜索结果链接里常带 &token=（防爬参数），打了码 Agent 就打不开链接
    (re.compile(r"(?i)(?P<k>\b(?:sessionid|sessionid_ss|sid_tt|sid_guard|uid_tt|passport_csrf_token|web_session"
                r"|ttwid|odin_tt|access_token|refresh_token|id_token|api_key|apikey|client_secret|app_secret"
                r"|appsecret|secret_key|password|passwd)\b[\"']?\s*[:=]\s*[\"']?)" + _NOT_MASKED
                + r"[A-Za-z0-9._~+/%|=-]{8,}"), r"\g<k>" + MASK),
    # Bearer 后面跟的是 token；跳过 WWW-Authenticate 里 Bearer realm="…" 这类参数
    (re.compile(r"(?i)(?P<k>\bbearer\s+)" + _NOT_MASKED + r"(?![A-Za-z_]+=)[A-Za-z0-9._~+/=-]{16,}"), r"\g<k>" + MASK),
    # 网址里的账号密码：http://user:pass@host
    (re.compile(r"(?P<k>\b[a-z][a-z0-9+.-]*://[^/\s:@\"']+:)" + _NOT_MASKED + r"[^/\s@\"']+(?=@)"), r"\g<k>" + MASK),
    # 有固定前缀的密钥
    (re.compile(r"\bgl(?:pat|oas|dt|rt|cbt|ptt|ft|imt|soat|ffct)-[A-Za-z0-9_-]{20,}"), MASK),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})"), MASK),
    (re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"), MASK),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), MASK),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), MASK),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), MASK),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"), MASK),
    # 大陆手机号（前后不能紧挨字母数字，避免误伤作品 ID、时间戳）
    (re.compile(r"(?<![0-9A-Za-z])(?:\+?86[- ]?)?1[3-9]\d{9}(?![0-9A-Za-z])"), PHONE_MASK),
]

KEYCHAIN_NAMES = ("tikhub", "gitlab", "redfox")
_SECRETS_TTL = 600
_secrets_cache: tuple[float, tuple[str, ...]] | None = None


def _known_secrets() -> tuple[str, ...]:
    """本系统自己用的密钥原值（钥匙串里的），出现在哪里都打码。缓存 10 分钟（钥匙串锁着时取不到，过会儿再试）。"""
    global _secrets_cache
    t = time.monotonic()
    if _secrets_cache and t - _secrets_cache[0] < _SECRETS_TTL:
        return _secrets_cache[1]
    from .secrets import keychain

    vals = []
    for name in KEYCHAIN_NAMES:
        try:
            v = keychain(name)
        except Exception:  # noqa: BLE001
            v = None
        if v and len(v) >= 8:
            vals.append(v)
    _secrets_cache = (t, tuple(sorted(set(vals), key=len, reverse=True)))
    return _secrets_cache[1]


def redact(text: str | None, *, secrets: tuple[str, ...] | None = None) -> tuple[str, int]:
    if not text:
        return text or "", 0
    n = 0
    for s in (_known_secrets() if secrets is None else secrets):
        if s in text:
            n += text.count(s)
            text = text.replace(s, MASK)
    for pat, rep in _RULES:
        text, k = pat.subn(rep, text)
        n += k
    return text, n


def redact_obj(obj, *, secrets: tuple[str, ...] | None = None):
    """返回 (打码后的对象, 打码处数)；dict / list 里的字符串逐个处理，其余原样返回。"""
    if isinstance(obj, str):
        return redact(obj, secrets=secrets)
    if isinstance(obj, dict):
        out, n = {}, 0
        for k, v in obj.items():
            out[k], c = redact_obj(v, secrets=secrets)
            n += c
        return out, n
    if isinstance(obj, (list, tuple)):
        items, n = [], 0
        for v in obj:
            v, c = redact_obj(v, secrets=secrets)
            items.append(v)
            n += c
        return (items if isinstance(obj, list) else tuple(items)), n
    return obj, 0


def scrub_file(p: Path, *, secrets: tuple[str, ...] | None = None, dry_run: bool = False) -> int:
    """文本文件就地打码；改动时原子替换，并保留原来的权限和修改时间。返回打码处数（dry_run 只数不改）。"""
    try:
        st = os.lstat(p)
    except OSError:
        return 0
    if not stat.S_ISREG(st.st_mode) or st.st_size == 0 or st.st_size > MAX_BYTES or p.suffix.lower() in SKIP_EXT:
        return 0
    try:
        raw = p.read_bytes()
    except OSError:
        return 0
    if b"\x00" in raw[:8192]:
        return 0  # 二进制
    text = raw.decode("utf-8", "surrogateescape")
    new, n = redact(text, secrets=secrets)
    if not n or dry_run:
        return n
    tmp = p.with_name(f".{p.name}.scrub-tmp")
    try:
        tmp.write_bytes(new.encode("utf-8", "surrogateescape"))
        os.chmod(tmp, stat.S_IMODE(st.st_mode))
        os.replace(tmp, p)
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    except OSError:
        tmp.unlink(missing_ok=True)
        return 0
    return n


def scrub_tree(root: Path, *, since: float | None = None, mark: bool = True,
               secrets: tuple[str, ...] | None = None, dry_run: bool = False) -> dict:
    """扫整个目录（不跟随软链接）。since：只扫这个时间戳之后改过的文件。返回 {files, hits}。"""
    root = Path(root)
    files = hits = 0
    if not root.is_dir() or root.is_symlink():
        return {"files": 0, "hits": 0}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            if name == MARKER or name.endswith(".scrub-tmp"):
                continue
            p = Path(dirpath) / name
            if since is not None:
                try:
                    if os.lstat(p).st_mtime < since:
                        continue
                except OSError:
                    continue
            n = scrub_file(p, secrets=secrets, dry_run=dry_run)
            if n:
                files += 1
                hits += n
    if mark and not dry_run:
        try:
            (root / MARKER).touch()
        except OSError:
            pass
    return {"files": files, "hits": hits}


def needs_scrub(root: Path) -> bool:
    """没有打码标记，或标记之后又有文件改过（重试、后来补写）。"""
    m = Path(root) / MARKER
    try:
        mt = m.stat().st_mtime
    except OSError:
        return True
    for dirpath, _dirs, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            if name == MARKER:
                continue
            try:
                if os.lstat(Path(dirpath) / name).st_mtime > mt:
                    return True
            except OSError:
                continue
    return False
