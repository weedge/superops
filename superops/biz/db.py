"""SQLite：ops.db（业务数据：内容、作品、指标、评论、需求、线索、漏斗、发布包）。WAL 模式。

宿主自己的状态（工单、触发、卡片）在 superops.sqlite，由 superops.core / superops.app 管，不在这里。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from ..infra import paths

OPS_DDL = """
CREATE TABLE IF NOT EXISTS contents (
  id TEXT PRIMARY KEY, parent_id TEXT, pillar TEXT, persona TEXT, topic TEXT,
  script_path TEXT, popto_features TEXT, status TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS posts (
  id TEXT PRIMARY KEY, content_id TEXT, platform TEXT, account TEXT,
  platform_post_id TEXT, url TEXT, title TEXT, pillar TEXT, published_at TEXT, channel_code TEXT,
  publish_mode TEXT, removed_at TEXT,
  UNIQUE (platform, platform_post_id)
);
CREATE TABLE IF NOT EXISTS metric_jobs (
  post_id TEXT, checkpoint TEXT, due_at TEXT, done_at TEXT, attempts INTEGER DEFAULT 0, last_error TEXT,
  PRIMARY KEY (post_id, checkpoint)
);
CREATE TABLE IF NOT EXISTS post_metrics (
  post_id TEXT, captured_at TEXT, checkpoint TEXT, source TEXT,
  views INTEGER, likes INTEGER, comments INTEGER, shares INTEGER, favorites INTEGER,
  follows_gained INTEGER, completion_rate REAL, avg_watch_sec REAL, profile_visits INTEGER,
  raw_ref TEXT, PRIMARY KEY (post_id, captured_at)
);
CREATE TABLE IF NOT EXISTS comments (
  id TEXT PRIMARY KEY, post_id TEXT, author_hash TEXT, text TEXT, likes INTEGER,
  created_at TEXT, category TEXT, sentiment TEXT, reply_draft TEXT, reply_status TEXT,
  purge_after TEXT, collected_at TEXT
);
CREATE TABLE IF NOT EXISTS signals (
  id TEXT PRIMARY KEY, source_type TEXT, source_ref TEXT, quote TEXT,
  kind TEXT, module TEXT, persona TEXT, intensity INTEGER, demand_id TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS demands (
  id TEXT PRIMARY KEY, title TEXT, kind TEXT, module TEXT, signal_count INTEGER,
  persona_dist TEXT, value_factor REAL, cost_size TEXT, score REAL,
  status TEXT, prd_path TEXT, mr_links TEXT, decided_ref TEXT, updated_at TEXT, is_test INTEGER, task TEXT
);
CREATE TABLE IF NOT EXISTS leads (
  id TEXT PRIMARY KEY, platform TEXT, profile_url TEXT, nickname TEXT, industry TEXT,
  lead_type TEXT, followers INTEGER, score REAL, stage TEXT, why TEXT, outreach_draft TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS funnel_daily (
  date TEXT, channel_code TEXT, clicks INTEGER, registrations INTEGER,
  first_done INTEGER, second_done INTEGER, paid INTEGER,
  PRIMARY KEY (date, channel_code)
);
CREATE TABLE IF NOT EXISTS mentions (
  id TEXT PRIMARY KEY, platform TEXT, post_id TEXT, url TEXT, title TEXT,
  author TEXT, author_id TEXT, profile_url TEXT, fans INTEGER,
  likes INTEGER, comments INTEGER, collects INTEGER,
  published_at TEXT, terms TEXT, categories TEXT, collected_at TEXT, source TEXT
);
CREATE TABLE IF NOT EXISTS competitor_comments (
  id TEXT PRIMARY KEY, platform TEXT, competitor TEXT, post_id TEXT, post_url TEXT, post_title TEXT,
  text TEXT, likes INTEGER, created_at TEXT, collected_at TEXT
);
CREATE TABLE IF NOT EXISTS competitor_works (
  id TEXT PRIMARY KEY, competitor TEXT, platform TEXT, post_id TEXT, url TEXT, title TEXT, author TEXT,
  likes INTEGER, comments INTEGER, published_at TEXT, collected_at TEXT
);
CREATE TABLE IF NOT EXISTS competitor_accounts (
  platform TEXT, account_id TEXT, day TEXT, competitor TEXT, name TEXT, url TEXT, fans INTEGER, verified TEXT,
  collected_at TEXT, PRIMARY KEY (platform, account_id, day)
);
CREATE TABLE IF NOT EXISTS deploy_checks (
  project TEXT, iid INTEGER, sha TEXT, url TEXT, merged_at TEXT, deadline TEXT,
  status TEXT,  -- pending|verified|mismatch|unconfigured
  checked_at TEXT, attempts INTEGER DEFAULT 0, note TEXT, web_url TEXT,
  PRIMARY KEY (project, iid)
);
CREATE TABLE IF NOT EXISTS publish_packs (
  id TEXT PRIMARY KEY, content_id TEXT, platform TEXT, account TEXT, title TEXT, scheduled_at TEXT,
  pack_dir TEXT, publish_mode TEXT, status TEXT,  -- ready|carded|approved|published|rejected|failed
  card_id TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS actions (
  action_id TEXT PRIMARY KEY, kind TEXT, ref TEXT, approved_at TEXT, status TEXT,
  result TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS listening_terms (
  term TEXT PRIMARY KEY, platform TEXT, industry TEXT, active INTEGER DEFAULT 1,
  hits_7d INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS feedback_inbox (
  id TEXT PRIMARY KEY, channel TEXT, link TEXT, received_at TEXT, read_at TEXT
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ops_db_path() -> Path:
    return paths.ops_data() / "ops.db"


# 已有库补列（CREATE TABLE IF NOT EXISTS 不会给老表加新列）
OPS_MIGRATIONS = [("comments", "collected_at", "TEXT"),
                  ("posts", "removed_at", "TEXT"),    # 作品在平台上删掉的时间：不再采集
                  ("demands", "is_test", "INTEGER"),  # 需求原话标了【测试】：卡片和 MR 上都标出来
                  ("demands", "task", "TEXT"),        # 负责它的开发工单目录名：后续几轮按它找回需求
                  ("contents", "task", "TEXT"),       # 排出这条选题的工单：工单结束时它还没批的选题标成搁置
                  ("contents", "planned_date", "TEXT"),   # 面板日历上挪过的计划发布日（没挪过就按内容 ID 里的日期）
                  ("contents", "removed_at", "TEXT"),     # 面板日历上删掉的时间（状态同时改成 deleted，不真删）
                  ("leads", "target_url", "TEXT"),    # 要评论的那条作品
                  ("leads", "comment_text", "TEXT"),  # 卡片上给人看、批了就照发的评论原文
                  ("leads", "comment_status", "TEXT"),  # sent / unconfirmed（点了发送没确认到）/ manual（要人去发）
                  ("leads", "commented_at", "TEXT"),
                  ("leads", "comment_note", "TEXT"),
                  ("listening_terms", "removed_at", "TEXT"),
                  ("competitor_comments", "post_author", "TEXT")]   # 评论所在作品的作者：评审卡片要写清证据来自哪条作品   # 在面板上删掉的词：留着这一行，免得 keywords.yaml 再同步回来


def open_ops(path: Path | None = None) -> sqlite3.Connection:
    conn = connect(path or ops_db_path())
    conn.executescript(OPS_DDL)
    for table, col, typ in OPS_MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    return conn


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]
