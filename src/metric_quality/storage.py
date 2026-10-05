"""统计资料质量服务的 SQLite 模式与事务辅助。

核心建模：

- ``policies``：冻结规则版本，只增不改，新版本必须显式接续旧版本；
- ``quality_records``：报送记录（报送主体/样本/指标/报告期/记录类型）；
- ``analyses``：以“规则版本 + 输入摘要”去重的不可变分析版本；
- ``decisions``：针对具体分析版本发布的决定，一经发布不可修改；
- ``legacy_analyses``：旧算法版本的历史分析，连同输入摘要原样保留；
- ``quality_events``：审计事件，支持从总体通过率逐层下钻。
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path


SCHEMA_VERSION = 2

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS policies (
    policy_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    indicator_id TEXT NOT NULL,
    supersedes_version INTEGER,
    canonical_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (policy_id, version),
    UNIQUE (content_sha256),
    FOREIGN KEY (policy_id, supersedes_version) REFERENCES policies(policy_id, version)
);

CREATE TABLE IF NOT EXISTS quality_records (
    record_id TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    reporter_id TEXT NOT NULL,
    sample_id TEXT NOT NULL,
    indicator_id TEXT NOT NULL,
    period TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('report', 'resubmission', 'missing', 'withdrawal')),
    value TEXT,
    source_id TEXT,
    recorded_at TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    imported_by TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (policy_id, policy_version, record_id),
    FOREIGN KEY (policy_id, policy_version) REFERENCES policies(policy_id, version)
);

CREATE INDEX IF NOT EXISTS quality_records_scope
ON quality_records(policy_id, policy_version, sample_id, period);

CREATE TABLE IF NOT EXISTS analyses (
    analysis_id INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_id TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    input_sha256 TEXT NOT NULL CHECK (length(input_sha256) = 64),
    algorithm_version TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (policy_id, policy_version, input_sha256),
    FOREIGN KEY (policy_id, policy_version) REFERENCES policies(policy_id, version)
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id INTEGER NOT NULL UNIQUE REFERENCES analyses(analysis_id),
    decision TEXT NOT NULL CHECK (decision IN ('release', 'hold', 'reject')),
    reason TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
    revoked_by TEXT,
    revoked_at TEXT,
    revoke_reason TEXT
);

CREATE TABLE IF NOT EXISTS legacy_analyses (
    legacy_id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    input_summary_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (lot_id, algorithm_version, input_summary_json)
);

CREATE TABLE IF NOT EXISTS quality_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS quality_events_entity
ON quality_events(entity_type, entity_id, event_id);
"""

REQUIRED_TABLES = frozenset({
    "schema_meta", "policies", "quality_records", "analyses",
    "decisions", "legacy_analyses", "quality_events", "users", "sessions",
})


def connect(path: str | Path = ":memory:", *, check_same_thread: bool = True) -> sqlite3.Connection:
    connection = sqlite3.connect(
        str(path), isolation_level=None, check_same_thread=check_same_thread
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def initialize(connection: sqlite3.Connection) -> None:
    """初始化质量模型表，重复执行不改变已有数据。"""

    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def inspect_schema(connection: sqlite3.Connection) -> dict[str, object]:
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
