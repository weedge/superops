"""Store：唯一打开 superops.sqlite 的地方。领域仓库、发件箱回执、副作用队列的表、键值记录、计数器、运行记录。

所有写入都在 atomic() 里：一把可重入锁 + 可嵌套的事务（SAVEPOINT）。after_commit 登记的动作只在最外层提交后执行，
回滚就丢掉——所以“先落库、再做外部动作”在这里成立。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable

from ..core.model import live_ticket, now_iso

log = logging.getLogger("superops.store")
SCHEMA_VERSION = 1
DDL = """
CREATE TABLE IF NOT EXISTS trigger (id TEXT PRIMARY KEY, source TEXT, at TEXT, human INTEGER, value TEXT);
CREATE TABLE IF NOT EXISTS disposition (id TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS trigger_thread (thread TEXT, at TEXT, id TEXT, human INTEGER, PRIMARY KEY (thread, at, id));
CREATE TABLE IF NOT EXISTS thread (key TEXT PRIMARY KEY, source TEXT, last_at TEXT, active INTEGER, subscribed INTEGER, value TEXT);
CREATE TABLE IF NOT EXISTS ticket (id TEXT PRIMARY KEY, no INTEGER, created_at TEXT, updated_at TEXT, open INTEGER, active INTEGER,
  rule TEXT, value TEXT);
CREATE TABLE IF NOT EXISTS decision (id TEXT PRIMARY KEY, thread TEXT, at TEXT, kind TEXT, value TEXT);
CREATE TABLE IF NOT EXISTS effect (kind TEXT, key TEXT, value TEXT, seq INTEGER, generation INTEGER DEFAULT 0,
  attempts INTEGER DEFAULT 0, error TEXT DEFAULT '', due REAL, since TEXT, PRIMARY KEY (kind, key));
CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, ticket TEXT, kind TEXT, status TEXT, at TEXT, settled_at TEXT,
  reason TEXT, value TEXT);
CREATE TABLE IF NOT EXISTS run (id TEXT PRIMARY KEY, ticket TEXT, assignment TEXT, role TEXT, agent TEXT, is_fallback INTEGER,
  started_at TEXT, finished_at TEXT, status TEXT, error_kind TEXT, error_detail TEXT, session_id TEXT, cost_usd REAL,
  duration_s REAL, dir TEXT, model TEXT, effort TEXT);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS counter (day TEXT, name TEXT, value REAL DEFAULT 0, PRIMARY KEY (day, name));
CREATE INDEX IF NOT EXISTS idx_thread_active ON thread(active) WHERE active = 1;
CREATE INDEX IF NOT EXISTS idx_ticket_open ON ticket(open) WHERE open = 1;
CREATE INDEX IF NOT EXISTS idx_ticket_active ON ticket(active) WHERE active = 1;
CREATE INDEX IF NOT EXISTS idx_ticket_no ON ticket(no);
CREATE INDEX IF NOT EXISTS idx_decision_thread ON decision(thread, at);
CREATE INDEX IF NOT EXISTS idx_trigger_at ON trigger(at);
CREATE INDEX IF NOT EXISTS idx_effect_due ON effect(kind, due);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status, at);
CREATE INDEX IF NOT EXISTS idx_run_ticket ON run(ticket);
"""


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _l(text: str | None) -> Any:
    return json.loads(text) if text else None


class _Triggers:
    def __init__(self, store: "Store"):
        self.s = store

    def get(self, key: str) -> dict | None:
        return _l(self.s.one("SELECT value FROM trigger WHERE id=?", (key,)))

    def save(self, trigger: dict) -> None:
        self.s.run("INSERT OR REPLACE INTO trigger(id, source, at, human, value) VALUES (?,?,?,?,?)",
                   (trigger["id"], trigger["source"], trigger["at"], int(bool(trigger.get("human"))), _j(trigger)))
        for ref in trigger["threads"]:
            self.s.run("INSERT OR IGNORE INTO trigger_thread(thread, at, id, human) VALUES (?,?,?,?)",
                       (f"{ref['source']}:{ref['id']}", trigger["at"], trigger["id"], int(bool(trigger.get("human")))))

    def disposition(self, key: str) -> dict | None:
        return _l(self.s.one("SELECT value FROM disposition WHERE id=?", (key,)))

    def save_disposition(self, key: str, value: dict) -> None:
        self.s.run("INSERT OR REPLACE INTO disposition(id, value) VALUES (?,?)", (key, _j(value)))


class _Threads:
    def __init__(self, store: "Store"):
        self.s = store

    def get(self, key: str) -> dict | None:
        return _l(self.s.one("SELECT value FROM thread WHERE key=?", (key,)))

    def save(self, thread: dict) -> None:
        active = int(bool(thread["pending"] or thread.get("evaluating")))
        self.s.run("INSERT INTO thread(key, source, last_at, active, subscribed, value) VALUES (?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                   "source=excluded.source, last_at=excluded.last_at, active=excluded.active, subscribed=excluded.subscribed, value=excluded.value",
                   (thread["key"], thread["ref"]["source"], thread.get("lastAt"), active, int(bool(thread["subscriptions"])), _j(thread)))

    def list(self) -> list[dict]:
        return [_l(r[0]) for r in self.s.all("SELECT value FROM thread ORDER BY IFNULL(last_at, '') DESC, rowid")]

    def active(self) -> list[dict]:
        return [_l(r[0]) for r in self.s.all("SELECT value FROM thread WHERE active=1 ORDER BY rowid")]


class _Tickets:
    def __init__(self, store: "Store"):
        self.s = store

    def get(self, key: str) -> dict | None:
        return _l(self.s.one("SELECT value FROM ticket WHERE id=?", (key,)))

    def by_no(self, no: int) -> dict | None:
        return _l(self.s.one("SELECT value FROM ticket WHERE no=?", (no,)))

    def save(self, ticket: dict) -> None:
        latest = ticket["assignments"][-1] if ticket["assignments"] else None
        active = int(bool(ticket["undelivered"]) or bool(latest and latest["state"] in ("pending", "running")))
        rule = ((ticket["mandate"].get("basis") or {}).get("rules") or [None])[0]
        self.s.run("INSERT INTO ticket(id, no, created_at, updated_at, open, active, rule, value) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                   "no=excluded.no, created_at=excluded.created_at, updated_at=excluded.updated_at, open=excluded.open, active=excluded.active, "
                   "rule=excluded.rule, value=excluded.value",
                   (ticket["id"], ticket.get("no"), ticket["createdAt"], ticket["updatedAt"], int(live_ticket(ticket)), active, rule, _j(ticket)))

    def list(self) -> list[dict]:
        return [_l(r[0]) for r in self.s.all("SELECT value FROM ticket ORDER BY rowid DESC")]

    def open(self) -> list[dict]:
        return [_l(r[0]) for r in self.s.all("SELECT value FROM ticket WHERE open=1 ORDER BY rowid")]

    def active(self) -> list[dict]:
        return [_l(r[0]) for r in self.s.all("SELECT value FROM ticket WHERE active=1 ORDER BY rowid")]

    def recent(self, limit: int = 50, *, open_: bool | None = None, since: str | None = None) -> list[dict]:
        where, args = [], []
        if open_ is not None:
            where.append("open=?")
            args.append(int(open_))
        if since:
            where.append("updated_at>=?")
            args.append(since)
        sql = "SELECT value FROM ticket" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY updated_at DESC LIMIT ?"
        return [_l(r[0]) for r in self.s.all(sql, (*args, limit))]


class _Decisions:
    def __init__(self, store: "Store"):
        self.s = store

    def get(self, key: str) -> dict | None:
        return _l(self.s.one("SELECT value FROM decision WHERE id=?", (key,)))

    def save(self, decision: dict) -> None:
        self.s.run("INSERT INTO decision(id, thread, at, kind, value) VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                   "thread=excluded.thread, at=excluded.at, kind=excluded.kind, value=excluded.value",
                   (decision["id"], decision["threadKey"], decision["at"], decision["kind"], _j(decision)))

    def by_thread(self, key: str) -> list[dict]:
        return [_l(r[0]) for r in self.s.all("SELECT value FROM decision WHERE thread=? ORDER BY rowid", (key,))]

    def recent(self, limit: int = 50, kind: str | None = None, since: str | None = None) -> list[dict]:
        where, args = [], []
        if kind:
            where.append("kind=?")
            args.append(kind)
        if since:
            where.append("at>=?")
            args.append(since)
        sql = "SELECT value FROM decision" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY at DESC LIMIT ?"
        return [_l(r[0]) for r in self.s.all(sql, (*args, limit))]


class _Receipts:
    """发件箱回执：每条消息一行，id 排过队就不会再排第二次。只有 Outbox 写它。"""

    def __init__(self, store: "Store"):
        self.s = store

    def get(self, key: str) -> dict | None:
        row = self.s.all("SELECT value, status, at, settled_at, reason FROM outbox WHERE id=?", (key,))
        if not row:
            return None
        value, status, at, settled, reason = row[0]
        return {**_l(value), "status": status, "at": at, "settledAt": settled, "reason": reason}

    def add(self, message: dict) -> bool:
        if self.s.one("SELECT 1 FROM outbox WHERE id=?", (message["id"],)):
            return False
        self.s.run("INSERT INTO outbox(id, ticket, kind, status, at, value) VALUES (?,?,?,?,?,?)",
                   (message["id"], message.get("ticketId"), message["kind"], "pending", now_iso(), _j(message)))
        return True

    def settle(self, key: str, status: str, reason: str | None = None) -> None:
        self.s.run("UPDATE outbox SET status=?, settled_at=?, reason=? WHERE id=?", (status, now_iso(), reason, key))

    def withdraw(self, key: str) -> bool:
        return self.s.run("UPDATE outbox SET status='dropped', settled_at=?, reason='withdrawn' WHERE id=? AND status='pending'",
                          (now_iso(), key)) > 0

    def behind(self, message: dict) -> bool:
        """同一张工单发往同一个会话的更早的消息还没发出去（且失败不到 3 次）。"""
        if not message.get("ticketId"):
            return False
        rows = self.s.all("SELECT o.id, o.value, IFNULL(e.attempts, 0) FROM outbox o LEFT JOIN effect e ON e.kind='reply' AND e.key=o.id "
                          "WHERE o.ticket=? AND o.status='pending' AND o.rowid < (SELECT rowid FROM outbox WHERE id=?) ORDER BY o.rowid",
                          (message["ticketId"], message["id"]))
        same = lambda c: json.dumps(c, sort_keys=True, ensure_ascii=False)  # noqa: E731
        channel = same(message["channel"])
        return any(same(_l(value)["channel"]) == channel and attempts < 3 for _id, value, attempts in rows)

    def list(self, status: str | None = None, limit: int = 50, since: str | None = None, kind: str | None = None) -> list[dict]:
        where, args = [], []
        for col, val in (("status", status), ("kind", kind)):
            if val:
                where.append(f"{col}=?")
                args.append(val)
        if since:
            where.append("at>=?")
            args.append(since)
        sql = ("SELECT value, status, at, settled_at, reason FROM outbox" + (" WHERE " + " AND ".join(where) if where else "")
               + " ORDER BY at DESC LIMIT ?")
        return [{**_l(v), "status": s, "at": a, "settledAt": d, "reason": r} for v, s, a, d, r in self.s.all(sql, (*args, limit))]

    def sent_today(self, kind: str, day_start: str) -> int:
        return int(self.s.one("SELECT COUNT(*) FROM outbox WHERE kind=? AND status='sent' AND settled_at>=?", (kind, day_start)) or 0)


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(DDL)
        have = {r[1] for r in self.db.execute("PRAGMA table_info(run)")}
        for col in ("model", "effort"):            # 2026-11-13 加的两列：这一轮实际用的模型和思考强度；旧库启动时补上
            if col not in have:
                self.db.execute(f"ALTER TABLE run ADD COLUMN {col} TEXT")
        self.lock = threading.RLock()
        self._depth = 0
        self._after: list[Callable[[], None]] = []
        self.triggers, self.threads = _Triggers(self), _Threads(self)
        self.tickets, self.decisions = _Tickets(self), _Decisions(self)
        self.receipts = _Receipts(self)
        version = self.get("schema")
        if version is None:
            self.set("schema", SCHEMA_VERSION)
        elif version > SCHEMA_VERSION:
            raise RuntimeError(f"数据库是更新的版本（schema {version}），这个程序只认到 {SCHEMA_VERSION}")

    # ---- 事务

    def atomic(self, work: Callable[[], Any]) -> Any:
        with self.lock:
            name = f"sp{self._depth}"
            if self._depth == 0:
                self.db.execute("BEGIN IMMEDIATE")
            else:
                self.db.execute(f"SAVEPOINT {name}")
            self._depth += 1
            mark = len(self._after)
            try:
                result = work()
            except BaseException:
                self._depth -= 1
                del self._after[mark:]
                self.db.execute("ROLLBACK" if self._depth == 0 else f"ROLLBACK TO {name}")
                if self._depth:
                    self.db.execute(f"RELEASE {name}")
                raise
            self._depth -= 1
            if self._depth:
                self.db.execute(f"RELEASE {name}")
                return result
            self.db.execute("COMMIT")
            after, self._after = self._after, []
        for action in after:
            try:
                action()
            except Exception:  # noqa: BLE001  事务已经提交：报告这个错误，其余动作照跑
                log.exception("提交后的动作出错")
        return result

    def after_commit(self, action: Callable[[], None]) -> None:
        """提交之后才执行；不在事务里时立刻执行。"""
        with self.lock:
            if self._depth:
                self._after.append(action)
                return
        action()

    def run(self, sql: str, args: tuple = ()) -> int:
        with self.lock:
            return self.db.execute(sql, args).rowcount

    def all(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple = ()) -> Any:
        with self.lock:
            row = self.db.execute(sql, args).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        with self.lock:
            self.db.close()

    # ---- 键值、计数器

    def get(self, key: str, default: Any = None) -> Any:
        value = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return default if value is None else json.loads(value)

    def set(self, key: str, value: Any) -> None:
        self.run("INSERT OR REPLACE INTO kv(key, value) VALUES (?,?)", (key, _j(value)))

    def delete(self, key: str) -> None:
        self.run("DELETE FROM kv WHERE key=?", (key,))

    def keys(self, prefix: str) -> list[str]:
        return [r[0] for r in self.all("SELECT key FROM kv WHERE key LIKE ? ORDER BY key", (prefix.replace("%", "") + "%",))]

    def next_no(self) -> int:
        def work():
            no = int(self.get("ticket-no", 0)) + 1
            self.set("ticket-no", no)
            return no
        return self.atomic(work)

    def add_counter(self, name: str, value: float, day: str) -> None:
        self.run("INSERT INTO counter(day, name, value) VALUES (?,?,?) ON CONFLICT(day, name) DO UPDATE SET value = value + excluded.value",
                 (day, name, value))

    def counter(self, name: str, day: str) -> float:
        return float(self.one("SELECT value FROM counter WHERE day=? AND name=?", (day, name)) or 0)

    # ---- 运行记录（每次拉起 Agent / 脚本一行）

    def save_run(self, run: dict) -> None:
        cols = ("id", "ticket", "assignment", "role", "agent", "model", "effort", "is_fallback", "started_at", "finished_at", "status",
                "error_kind", "error_detail", "session_id", "cost_usd", "duration_s", "dir")
        self.run(f"INSERT OR REPLACE INTO run({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                 tuple(run.get(c) for c in cols))

    def runs(self, ticket: str | None = None, limit: int = 50, since: str | None = None) -> list[dict]:
        cols = ("id", "ticket", "assignment", "role", "agent", "model", "effort", "is_fallback", "started_at", "finished_at", "status",
                "error_kind", "error_detail", "session_id", "cost_usd", "duration_s", "dir")
        where, args = [], []
        if ticket:
            where.append("ticket=?")
            args.append(ticket)
        if since:
            where.append("started_at>=?")
            args.append(since)
        sql = (f"SELECT {', '.join(cols)} FROM run" + (" WHERE " + " AND ".join(where) if where else "")
               + " ORDER BY started_at DESC LIMIT ?")
        return [dict(zip(cols, row)) for row in self.all(sql, (*args, limit))]

    # ---- 只读查询（视图、来源、工具用；从不写）

    def thread_triggers(self, key: str, limit: int = 50) -> list[dict]:
        rows = self.all("SELECT t.value FROM trigger_thread x JOIN trigger t ON t.id = x.id WHERE x.thread=? ORDER BY x.at DESC, x.id DESC LIMIT ?",
                        (key, limit))
        return [_l(r[0]) for r in rows]

    def recent_triggers(self, limit: int = 50, since: str | None = None, source: str | None = None, human: bool | None = None) -> list[dict]:
        where, args = [], []
        if human is not None:
            where.append("human=?")
            args.append(1 if human else 0)
        if since:
            where.append("at>=?")
            args.append(since)
        if source:
            where.append("source=?")
            args.append(source)
        sql = "SELECT value FROM trigger" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY at DESC LIMIT ?"
        return [_l(r[0]) for r in self.all(sql, (*args, limit))]

    def trigger_stats(self, since: str) -> dict:
        return {source: n for source, n in self.all("SELECT source, COUNT(*) FROM trigger WHERE at>=? GROUP BY source", (since,))}

    def tickets_by_rule(self, rule: str, thread: str | None = None) -> list[dict]:
        tickets = [_l(r[0]) for r in self.all("SELECT value FROM ticket WHERE rule=? ORDER BY created_at", (rule,))]
        if thread is None:
            return tickets
        subscribed = set((self.threads.get(thread) or {}).get("subscriptions") or [])
        return [t for t in tickets if t["id"] in subscribed]

    def blank_text(self, before: str, dry_run: bool = False) -> dict:
        """数据保留：清空 before 之前的触发正文、已结束工单的输入 / 报告正文、已结清消息的正文；元数据永久保留。"""
        counts = {"triggers": 0, "tickets": 0, "messages": 0}

        def work():
            for (value,) in self.all("SELECT value FROM trigger WHERE at<?", (before,)):
                trigger = _l(value)
                if trigger.get("text") or (trigger.get("meta") or {}).get("data"):
                    counts["triggers"] += 1
                    if not dry_run:
                        trigger["text"] = ""
                        trigger.get("meta", {}).pop("data", None)
                        self.run("UPDATE trigger SET value=? WHERE id=?", (_j(trigger), trigger["id"]))
            for (value,) in self.all("SELECT value FROM ticket WHERE open=0 AND updated_at<?", (before,)):
                ticket = _l(value)
                if ticket.get("blanked"):
                    continue
                counts["tickets"] += 1
                if not dry_run:
                    for item in ticket["inputs"]:
                        for field in ("text", "note"):
                            if item.get(field):
                                item[field] = ""
                    for item in [*ticket["inputs"], *(ticket.get("answered") or [])]:
                        if item.get("reason"):
                            item["reason"] = ""
                    for assignment in ticket["assignments"]:
                        if assignment.get("report"):
                            assignment["report"]["summary"] = ""
                    ticket["blanked"] = True
                    self.run("UPDATE ticket SET value=? WHERE id=?", (_j(ticket), ticket["id"]))
            for mid, value in self.all("SELECT id, value FROM outbox WHERE status!='pending' AND at<?", (before,)):
                message = _l(value)
                if message.get("text"):
                    counts["messages"] += 1
                    if not dry_run:
                        message["text"] = ""
                        self.run("UPDATE outbox SET value=? WHERE id=?", (_j(message), mid))
        self.atomic(work)
        return counts
