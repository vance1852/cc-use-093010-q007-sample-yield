"""统计样本批次、观测导入、质量分析与发布决定的应用服务。"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from .analytics import ALGORITHM_VERSION, SeriesRecord, analyze_batch
from .clock import SystemClock, isoformat
from .contracts import ObservationInput, RuleSet, ValidationError
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, content_digest
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "operator": {"source.register", "batch.create", "observation.import"},
    "analyst": {"ruleset.publish", "batch.freeze", "analysis.run", "report.read"},
    "approver": {"decision.publish", "report.read"},
    "auditor": {"report.read", "audit.read"},
}

DECISIONS = {"approved_for_aggregation", "rejected", "needs_more_data"}


class MetricQualityService:
    """在单个 SQLite 连接上提供全部业务操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return isoformat(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT user_id, display_name, role, active FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"用户不存在: {user_id}")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self, entity_type: str, entity_id: str, event_type: str, actor_id: str, payload: Mapping[str, Any]
    ) -> None:
        self.connection.execute(
            "INSERT INTO audit_events(entity_type,entity_id,event_type,actor_id,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (entity_type, entity_id, event_type, actor_id, canonical_json(payload), self._now()),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed(f"未知角色: {role}")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO users(user_id,display_name,role) VALUES(?,?,?)",
                    (user_id.strip(), display_name.strip(), role),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"用户已存在: {user_id}") from exc
        return {"user_id": user_id.strip(), "role": role}

    def register_source(
        self, actor_id: str, source_id: str, display_name: str, organization: str
    ) -> dict[str, Any]:
        self._require(actor_id, "source.register")
        if not source_id.strip() or not display_name.strip() or not organization.strip():
            raise ValidationFailed("报送主体编号、名称和机构不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO sources(source_id,display_name,organization,registered_by,registered_at) "
                    "VALUES(?,?,?,?,?)",
                    (source_id.strip(), display_name.strip(), organization.strip(), actor_id, self._now()),
                )
                self._audit(
                    "source", source_id.strip(), "source.registered", actor_id,
                    {"display_name": display_name.strip()},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"报送主体已存在: {source_id}") from exc
        return {"source_id": source_id.strip(), "display_name": display_name.strip()}

    def publish_rule_set(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "ruleset.publish")
        try:
            rule_set = RuleSet.from_dict(raw)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        text = canonical_json(raw)
        digest = content_digest([raw])
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO rule_sets(rule_set_id,version,title,canonical_json,content_sha256,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (rule_set.rule_set_id, rule_set.version, rule_set.title, text, digest, actor_id, self._now()),
                )
                identity = f"{rule_set.rule_set_id}@{rule_set.version}"
                self._audit("rule_set", identity, "rule_set.published", actor_id, {"sha256": digest})
        except sqlite3.IntegrityError as exc:
            raise Conflict("规则集版本或内容摘要已经存在") from exc
        return {"rule_set_id": rule_set.rule_set_id, "version": rule_set.version, "sha256": digest}

    def _rule_set(self, rule_set_id: str, version: int) -> tuple[RuleSet, str]:
        row = self.connection.execute(
            "SELECT canonical_json,content_sha256 FROM rule_sets WHERE rule_set_id=? AND version=?",
            (rule_set_id, version),
        ).fetchone()
        if row is None:
            raise NotFound("规则集版本不存在")
        return RuleSet.from_dict(json.loads(row["canonical_json"])), row["content_sha256"]

    def create_batch(
        self,
        actor_id: str,
        batch_id: str,
        rule_set_id: str,
        rule_set_version: int,
        sample_ids: Sequence[str],
    ) -> dict[str, Any]:
        self._require(actor_id, "batch.create")
        if not batch_id.strip():
            raise ValidationFailed("批次编号不能为空")
        self._rule_set(rule_set_id, rule_set_version)
        if isinstance(sample_ids, (str, bytes)) or not isinstance(sample_ids, Sequence):
            raise ValidationFailed("样本清单必须是非空数组")
        samples: list[str] = []
        for item in sample_ids:
            if not isinstance(item, str) or not item.strip():
                raise ValidationFailed("样本编号必须是非空字符串")
            samples.append(item.strip())
        if not samples:
            raise ValidationFailed("样本清单不能为空")
        if len(set(samples)) != len(samples):
            raise ValidationFailed("样本编号不能重复")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO batches(batch_id,rule_set_id,rule_set_version,state,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (batch_id.strip(), rule_set_id, rule_set_version, "open", actor_id, self._now()),
                )
                for position, sample_id in enumerate(samples):
                    self.connection.execute(
                        "INSERT INTO batch_samples(batch_id,sample_id,position) VALUES(?,?,?)",
                        (batch_id.strip(), sample_id, position),
                    )
                self._audit(
                    "batch", batch_id.strip(), "batch.created", actor_id,
                    {
                        "rule_set_id": rule_set_id,
                        "rule_set_version": rule_set_version,
                        "samples": samples,
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("批次编号冲突或规则集版本不存在") from exc
        return self.get_batch(batch_id.strip())

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFound("批次不存在")
        samples = self.connection.execute(
            "SELECT sample_id FROM batch_samples WHERE batch_id=? ORDER BY position", (batch_id,)
        ).fetchall()
        return dict(row) | {"sample_ids": [item["sample_id"] for item in samples]}

    def _idempotent_response(self, scope: str, key: str, request_digest: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT request_sha256,response_json FROM idempotency_keys WHERE scope=? AND key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_digest:
            raise Conflict("同一幂等键对应了不同请求内容")
        return json.loads(row["response_json"])

    def import_observations(
        self,
        actor_id: str,
        batch_id: str,
        idempotency_key: str,
        raw_rows: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        self._require(actor_id, "observation.import")
        rows = tuple(raw_rows)
        if not rows:
            raise ValidationFailed("观测记录数组不能为空")
        if not idempotency_key.strip():
            raise ValidationFailed("幂等键不能为空")
        request_digest = content_digest(rows)
        scope = f"observations:{batch_id}"
        existing = self._idempotent_response(scope, idempotency_key, request_digest)
        if existing is not None:
            return existing
        batch = self.get_batch(batch_id)
        if batch["state"] != "open":
            raise InvalidState("只有采集中的批次可以导入观测记录")
        rule_set, _ = self._rule_set(batch["rule_set_id"], batch["rule_set_version"])
        parsed: list[ObservationInput] = []
        for index, raw in enumerate(rows):
            try:
                parsed.append(ObservationInput.from_dict(raw, f"observations[{index}]"))
            except ValidationError as exc:
                raise ValidationFailed(str(exc)) from exc
        active_sources = {
            row["source_id"]
            for row in self.connection.execute("SELECT source_id FROM sources WHERE active=1")
        }
        allowed_sources = set(rule_set.freeze_rules.source_priority)
        sample_ids = set(batch["sample_ids"])
        expected_periods = set(rule_set.expected_periods)
        for item in parsed:
            if item.source_id not in active_sources:
                raise ValidationFailed(f"报送主体未注册或已停用: {item.source_id}")
            if item.source_id not in allowed_sources:
                raise ValidationFailed(f"报送主体不在规则集来源清单中: {item.source_id}")
            if item.sample_id not in sample_ids:
                raise ValidationFailed(f"观测样本不在批次样本清单中: {item.sample_id}")
            if item.period not in expected_periods:
                raise ValidationFailed(f"观测期间不在规则集期间清单中: {item.period}")
        latest_revision: dict[tuple[str, str, str], int] = {}
        for row in self.connection.execute(
            "SELECT source_id,sample_id,period,MAX(revision) AS max_revision FROM observation_records "
            "WHERE batch_id=? GROUP BY source_id,sample_id,period",
            (batch_id,),
        ):
            latest_revision[(row["source_id"], row["sample_id"], row["period"])] = row["max_revision"]
        next_revision = dict(latest_revision)
        for item in parsed:
            key = (item.source_id, item.sample_id, item.period)
            required = next_revision.get(key, 0) + 1
            if item.revision != required:
                raise ValidationFailed(
                    f"观测 {item.source_id}/{item.sample_id}/{item.period} 的复报版本必须为 {required}"
                )
            next_revision[key] = item.revision
        response = {"batch_id": batch_id, "inserted": len(parsed), "request_sha256": request_digest}
        try:
            with transaction(self.connection, immediate=True):
                for item, raw in zip(parsed, rows):
                    self.connection.execute(
                        "INSERT INTO observation_records(batch_id,source_id,sample_id,period,revision,kind,"
                        "value,content_sha256,imported_by,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            batch_id,
                            item.source_id,
                            item.sample_id,
                            item.period,
                            item.revision,
                            item.kind,
                            None if item.value is None else format(item.value, "f"),
                            content_digest([raw]),
                            actor_id,
                            self._now(),
                        ),
                    )
                self.connection.execute(
                    "INSERT INTO idempotency_keys(scope,key,request_sha256,response_json,created_at) VALUES(?,?,?,?,?)",
                    (scope, idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit("batch", batch_id, "observations.imported", actor_id, response)
        except sqlite3.IntegrityError as exc:
            raise Conflict("观测记录来源行重复或幂等键并发冲突") from exc
        return response

    def freeze_batch(self, actor_id: str, batch_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "batch.freeze")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE batches SET state='frozen',revision=revision+1,frozen_at=? "
                "WHERE batch_id=? AND state='open' AND revision=?",
                (self._now(), batch_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("批次不是待冻结的当前版本")
            self._audit("batch", batch_id, "batch.frozen", actor_id, {"from_revision": expected_revision})
        return self.get_batch(batch_id)

    def _series_records(self, batch_id: str) -> tuple[SeriesRecord, ...]:
        rows = self.connection.execute(
            "SELECT sample_id,source_id,period,revision,kind,value FROM observation_records "
            "WHERE batch_id=? ORDER BY sample_id,period,source_id,revision",
            (batch_id,),
        ).fetchall()
        return tuple(
            SeriesRecord(
                sample_id=row["sample_id"],
                source_id=row["source_id"],
                period=row["period"],
                revision=row["revision"],
                kind=row["kind"],
                value=None if row["value"] is None else Decimal(row["value"]),
            )
            for row in rows
        )

    def run_analysis(
        self, actor_id: str, batch_id: str, rule_set_version: int | None = None
    ) -> dict[str, Any]:
        self._require(actor_id, "analysis.run")
        batch = self.get_batch(batch_id)
        if batch["state"] != "frozen":
            raise InvalidState("批次冻结后才能执行质量分析")
        pinned = batch["rule_set_version"]
        if rule_set_version is None:
            version = pinned
        else:
            if isinstance(rule_set_version, bool) or not isinstance(rule_set_version, int):
                raise ValidationFailed("规则版本必须是整数")
            version = rule_set_version
        if version < pinned:
            raise ValidationFailed("分析规则版本不能早于批次采集绑定的规则版本")
        rule_set, rule_set_sha = self._rule_set(batch["rule_set_id"], version)
        records = self._series_records(batch_id)
        snapshot: list[object] = [
            {
                "algorithm_version": ALGORITHM_VERSION,
                "rule_set_sha256": rule_set_sha,
                "sample_ids": batch["sample_ids"],
            }
        ]
        snapshot.extend(
            {
                "sample_id": record.sample_id,
                "period": record.period,
                "source_id": record.source_id,
                "revision": record.revision,
                "kind": record.kind,
                "value": None if record.value is None else format(record.value, "f"),
            }
            for record in records
        )
        input_digest = content_digest(snapshot)
        existing = self.connection.execute(
            "SELECT * FROM analyses WHERE batch_id=? AND rule_set_version=? AND algorithm_version=? AND input_sha256=?",
            (batch_id, version, ALGORITHM_VERSION, input_digest),
        ).fetchone()
        if existing is not None:
            return {
                "analysis_id": existing["analysis_id"],
                "batch_id": batch_id,
                "rule_set_version": version,
                "algorithm_version": existing["algorithm_version"],
                "input_sha256": input_digest,
                "supersedes_analysis_id": existing["supersedes_analysis_id"],
                "replayed": True,
                "result": json.loads(existing["result_json"]),
            }
        result = analyze_batch(rule_set, rule_set_sha, batch["sample_ids"], records)
        latest = self.connection.execute(
            "SELECT analysis_id FROM analyses WHERE batch_id=? ORDER BY analysis_id DESC LIMIT 1",
            (batch_id,),
        ).fetchone()
        supersedes = None if latest is None else latest["analysis_id"]
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO analyses(batch_id,rule_set_id,rule_set_version,algorithm_version,input_sha256,"
                    "result_json,supersedes_analysis_id,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        batch_id,
                        rule_set.rule_set_id,
                        version,
                        ALGORITHM_VERSION,
                        input_digest,
                        canonical_json(result),
                        supersedes,
                        actor_id,
                        self._now(),
                    ),
                )
                analysis_id = cursor.lastrowid
                self._audit(
                    "batch",
                    batch_id,
                    "analysis.completed",
                    actor_id,
                    {
                        "analysis_id": analysis_id,
                        "input_sha256": input_digest,
                        "algorithm_version": ALGORITHM_VERSION,
                        "rule_set_version": version,
                        "supersedes_analysis_id": supersedes,
                        "conclusions": result["conclusions"],
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("相同输入的分析版本已存在") from exc
        return {
            "analysis_id": analysis_id,
            "batch_id": batch_id,
            "rule_set_version": version,
            "algorithm_version": ALGORITHM_VERSION,
            "input_sha256": input_digest,
            "supersedes_analysis_id": supersedes,
            "replayed": False,
            "result": result,
        }

    def publish_decision(
        self, actor_id: str, batch_id: str, analysis_id: int, decision: str, reason: str
    ) -> dict[str, Any]:
        self._require(actor_id, "decision.publish")
        if decision not in DECISIONS:
            raise ValidationFailed("未知的发布决定")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationFailed("决定理由不能为空")
        analysis = self.connection.execute(
            "SELECT * FROM analyses WHERE analysis_id=? AND batch_id=?", (analysis_id, batch_id)
        ).fetchone()
        if analysis is None:
            raise NotFound("分析版本不存在")
        latest = self.connection.execute(
            "SELECT analysis_id FROM analyses WHERE batch_id=? ORDER BY analysis_id DESC LIMIT 1",
            (batch_id,),
        ).fetchone()
        if latest["analysis_id"] != analysis["analysis_id"]:
            raise InvalidState("只能针对最新分析版本发布决定")
        if analysis["created_by"] == actor_id:
            raise Forbidden("分析负责人不能发布自己分析的决定")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO decisions(batch_id,analysis_id,decision,reason,decided_by,decided_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (batch_id, analysis_id, decision, reason.strip(), actor_id, self._now()),
                )
                self._audit(
                    "batch",
                    batch_id,
                    "decision.published",
                    actor_id,
                    {"decision_id": cursor.lastrowid, "analysis_id": analysis_id, "decision": decision},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("该分析版本已经发布过决定") from exc
        return {
            "decision_id": cursor.lastrowid,
            "batch_id": batch_id,
            "analysis_id": analysis_id,
            "decision": decision,
        }

    @staticmethod
    def _analysis_dict(row: sqlite3.Row, with_result: bool) -> dict[str, Any]:
        data: dict[str, Any] = {
            "analysis_id": row["analysis_id"],
            "batch_id": row["batch_id"],
            "rule_set_id": row["rule_set_id"],
            "rule_set_version": row["rule_set_version"],
            "algorithm_version": row["algorithm_version"],
            "input_sha256": row["input_sha256"],
            "supersedes_analysis_id": row["supersedes_analysis_id"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }
        if with_result:
            data["result"] = json.loads(row["result_json"])
        return data

    def get_analysis(self, actor_id: str, analysis_id: int) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        row = self.connection.execute(
            "SELECT * FROM analyses WHERE analysis_id=?", (analysis_id,)
        ).fetchone()
        if row is None:
            raise NotFound("分析版本不存在")
        return self._analysis_dict(row, with_result=True)

    def get_sample_conclusion(self, actor_id: str, analysis_id: int, sample_id: str) -> dict[str, Any]:
        analysis = self.get_analysis(actor_id, analysis_id)
        for sample in analysis["result"]["samples"]:
            if sample["sample_id"] == sample_id:
                return {"analysis_id": analysis_id, **sample}
        raise NotFound(f"分析版本中不存在样本: {sample_id}")

    def report(self, actor_id: str, batch_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        batch = self.get_batch(batch_id)
        rule_set, rule_set_sha = self._rule_set(batch["rule_set_id"], batch["rule_set_version"])
        analyses = self.connection.execute(
            "SELECT * FROM analyses WHERE batch_id=? ORDER BY analysis_id", (batch_id,)
        ).fetchall()
        decisions = self.connection.execute(
            "SELECT * FROM decisions WHERE batch_id=? ORDER BY decision_id", (batch_id,)
        ).fetchall()
        events = self.connection.execute(
            "SELECT event_type,actor_id,payload_json,created_at FROM audit_events "
            "WHERE entity_type='batch' AND entity_id=? ORDER BY event_id",
            (batch_id,),
        ).fetchall()
        return {
            "batch": batch,
            "rule_set": {
                "rule_set_id": rule_set.rule_set_id,
                "version": rule_set.version,
                "title": rule_set.title,
                "sha256": rule_set_sha,
            },
            "analyses": [self._analysis_dict(row, with_result=False) for row in analyses],
            "latest_analysis": None if not analyses else self._analysis_dict(analyses[-1], with_result=True),
            "decisions": [dict(row) for row in decisions],
            "current_decision": None if not decisions else dict(decisions[-1]),
            "events": [dict(row) | {"payload": json.loads(row["payload_json"])} for row in events],
        }

    def audit_trail(self, actor_id: str, batch_id: str) -> list[dict[str, Any]]:
        self._require(actor_id, "audit.read")
        self.get_batch(batch_id)
        rows = self.connection.execute(
            "SELECT * FROM audit_events WHERE entity_type='batch' AND entity_id=? ORDER BY event_id",
            (batch_id,),
        ).fetchall()
        return [dict(row) | {"payload": json.loads(row["payload_json"])} for row in rows]
