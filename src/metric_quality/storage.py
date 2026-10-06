"""统计资料质量服务的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path


SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('operator', 'analyst', 'approver', 'auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    organization TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    registered_by TEXT NOT NULL REFERENCES users(user_id),
    registered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_sets (
    rule_set_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    title TEXT NOT NULL,
    canonical_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (rule_set_id, version),
    UNIQUE (content_sha256)
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    rule_set_id TEXT NOT NULL,
    rule_set_version INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('open', 'frozen')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    frozen_at TEXT,
    FOREIGN KEY (rule_set_id, rule_set_version) REFERENCES rule_sets(rule_set_id, version)
);

CREATE TABLE IF NOT EXISTS batch_samples (
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    sample_id TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    PRIMARY KEY (batch_id, sample_id),
    UNIQUE (batch_id, position)
);

CREATE TABLE IF NOT EXISTS observation_records (
    record_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    sample_id TEXT NOT NULL,
    period TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    kind TEXT NOT NULL CHECK (kind IN ('report', 'retraction')),
    value TEXT,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    imported_by TEXT NOT NULL REFERENCES users(user_id),
    imported_at TEXT NOT NULL,
    UNIQUE (batch_id, source_id, sample_id, period, revision)
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);

CREATE TABLE IF NOT EXISTS analyses (
    analysis_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    rule_set_id TEXT NOT NULL,
    rule_set_version INTEGER NOT NULL,
    algorithm_version TEXT NOT NULL,
    input_sha256 TEXT NOT NULL CHECK (length(input_sha256) = 64),
    result_json TEXT NOT NULL,
    supersedes_analysis_id INTEGER REFERENCES analyses(analysis_id),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE (batch_id, rule_set_version, algorithm_version, input_sha256)
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    analysis_id INTEGER NOT NULL REFERENCES analyses(analysis_id),
    decision TEXT NOT NULL CHECK (decision IN ('approved_for_aggregation', 'rejected', 'needs_more_data')),
    reason TEXT NOT NULL,
    decided_by TEXT NOT NULL REFERENCES users(user_id),
    decided_at TEXT NOT NULL,
    UNIQUE (batch_id, analysis_id)
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

REQUIRED_TABLES = frozenset({
    "schema_meta", "users", "sources", "rule_sets", "batches", "batch_samples",
    "observation_records", "idempotency_keys", "analyses", "decisions", "audit_events",
})


def connect(path: str | Path) -> sqlite3.Connection:
    """打开连接并启用严格的事务与外键设置。

    check_same_thread=False 允许 ThreadingHTTPServer 的工作线程共享连接；
    调用方通过 JsonApplication 的锁串行化请求，写事务由 BEGIN IMMEDIATE 保护。
    """

    connection = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    """显式事务；异常时保证回滚。"""

    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def initialize(connection: sqlite3.Connection) -> None:
    """初始化基础结构，重复执行不改变已有数据。"""

    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def inspect_schema(connection: sqlite3.Connection) -> dict[str, object]:
    """返回适合机器检查的数据库结构摘要。"""

    table_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = tuple(row["name"] for row in table_rows)
    version_row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    missing = sorted(REQUIRED_TABLES - set(tables))
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    return {
        "tables": tables,
        "missing_tables": missing,
        "schema_version": None if version_row is None else version_row["value"],
        "foreign_keys": bool(foreign_keys),
    }
