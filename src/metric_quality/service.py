"""统计资料质量的应用服务。

关键保证：

- 规则版本只增不改，新版本必须通过 ``supersedes_version`` 显式接续；
- 分析以“规则版本 + 输入摘要”为身份去重，是不可变版本；
- 决定一经发布不可静默修改，只能通过撤销留下审计痕迹，
  新决定必须落在后继分析版本上；
- 旧算法的历史分析连同输入摘要原样登记，永不被新算法覆盖；
- 每个错误都携带业务代码与可理解的中文说明。
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .auth import Auth
from .clock import SystemClock, isoformat
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, content_digest
from .quality import (
    ALGORITHM_VERSION,
    FrozenPolicy,
    ObservationRecord,
    QualityInputError,
    evaluate,
    field_canonical_tuple,
)
from .storage import connect, initialize, transaction

#: 旧批次算法的版本标识。其产出以历史版本形式保留，不参与新模型计算。
LEGACY_ALGORITHM_VERSION = "metric-quality-observation-yield/1"


class MetricQualityService:
    def __init__(self, database: str = ":memory:", clock=None, *, check_same_thread: bool = True) -> None:
        self.db = connect(database, check_same_thread=check_same_thread)
        initialize(self.db)
        self.auth = Auth(self.db)
        self.clock = clock or SystemClock()

    def _now(self) -> str:
        return isoformat(self.clock.now())

    def _audit(
        self, entity_type: str, entity_id: str, event_type: str, actor: str, payload: Mapping[str, Any]
    ) -> None:
        self.db.execute(
            "INSERT INTO quality_events(entity_type,entity_id,event_type,actor_id,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (entity_type, entity_id, event_type, actor, canonical_json(payload), self._now()),
        )

    def bootstrap_admin(self, user_id: str = "admin", password: str = "metric-admin") -> None:
        try:
            self.auth.create_user(user_id, password, "admin")
        except ValueError:
            pass

    # -- 冻结规则版本 -----------------------------------------------------

    def publish_policy(self, token: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        actor = self.auth.require(token, "policy.publish")
        try:
            policy = FrozenPolicy.from_dict(raw)
        except QualityInputError as exc:
            raise ValidationFailed(str(exc)) from exc
        if policy.supersedes_version is not None:
            parent = self.db.execute(
                "SELECT 1 FROM policies WHERE policy_id=? AND version=?",
                (policy.policy_id, policy.supersedes_version),
            ).fetchone()
            if parent is None:
                raise ValidationFailed(
                    f"接续的规则版本 {policy.policy_id}@{policy.supersedes_version} 不存在"
                )
        text = canonical_json(policy.to_dict())
        digest = content_digest([policy.to_dict()])
        try:
            with transaction(self.db, immediate=True):
                self.db.execute(
                    "INSERT INTO policies(policy_id,version,indicator_id,supersedes_version,"
                    "canonical_json,content_sha256,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (policy.policy_id, policy.version, policy.indicator_id,
                     policy.supersedes_version, text, digest, actor.user_id, self._now()),
                )
                self._audit(
                    "policy", f"{policy.policy_id}@{policy.version}", "policy.published",
                    actor.user_id,
                    {"supersedes_version": policy.supersedes_version, "sha256": digest},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("规则版本已存在或内容摘要冲突；新版本只能作为后继版本追加") from exc
        return self.get_policy(token, policy.policy_id, policy.version)

    def _load_policy(self, policy_id: str, version: int) -> FrozenPolicy:
        row = self.db.execute(
            "SELECT canonical_json FROM policies WHERE policy_id=? AND version=?", (policy_id, version)
        ).fetchone()
        if row is None:
            raise NotFound(f"规则版本不存在: {policy_id}@{version}")
        return FrozenPolicy.from_dict(json.loads(row["canonical_json"]))

    def get_policy(self, token: str, policy_id: str, version: int | None = None) -> dict[str, Any]:
        self.auth.require(token, "read")
        if version is None:
            row = self.db.execute(
                "SELECT * FROM policies WHERE policy_id=? ORDER BY version DESC LIMIT 1", (policy_id,)
            ).fetchone()
        else:
            row = self.db.execute(
                "SELECT * FROM policies WHERE policy_id=? AND version=?", (policy_id, version)
            ).fetchone()
        if row is None:
            hint = "" if version is None else f"版本不存在: {policy_id}@{version}"
            raise NotFound(hint or f"规则不存在: {policy_id}")
        return {
            "policy_id": row["policy_id"],
            "version": row["version"],
            "indicator_id": row["indicator_id"],
            "supersedes_version": row["supersedes_version"],
            "content_sha256": row["content_sha256"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "policy": json.loads(row["canonical_json"]),
        }

    def list_policies(self, token: str, policy_id: str) -> list[dict[str, Any]]:
        self.auth.require(token, "read")
        rows = self.db.execute(
            "SELECT policy_id,version,indicator_id,supersedes_version,content_sha256,created_at "
            "FROM policies WHERE policy_id=? ORDER BY version", (policy_id,)
        ).fetchall()
        if not rows:
            raise NotFound(f"规则不存在: {policy_id}")
        return [dict(row) for row in rows]

    # -- 报送记录 ---------------------------------------------------------

    def import_records(
        self,
        token: str,
        policy_id: str,
        version: int,
        raw_records: Iterable[Mapping[str, Any]],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        actor = self.auth.require(token, "record.import")
        rows = tuple(raw_records)
        if not rows:
            raise ValidationFailed("报送记录数组不能为空")
        policy = self._load_policy(policy_id, version)

        parsed: list[tuple[dict[str, Any], ObservationRecord, str]] = []
        seen_ids: set[str] = set()
        for index, raw in enumerate(rows):
            if not isinstance(raw, Mapping):
                raise ValidationFailed(f"records[{index}] 必须是 JSON 对象")
            try:
                record = ObservationRecord.from_dict(raw)
            except QualityInputError as exc:
                raise ValidationFailed(f"records[{index}] {exc}") from exc
            if record.record_id is None:
                raise ValidationFailed(f"records[{index}].record_id 不能为空，记录需要稳定身份")
            if record.record_id in seen_ids:
                raise ValidationFailed(f"records[{index}] 的 record_id={record.record_id} 在本次导入中重复")
            if record.indicator_id != policy.indicator_id:
                raise ValidationFailed(
                    f"records[{index}] 指标 {record.indicator_id} 与冻结规则指标 {policy.indicator_id} 不一致"
                )
            if record.reporter_id not in policy.reporter_ids:
                raise ValidationFailed(
                    f"records[{index}] 报送主体 {record.reporter_id} 未在规则中登记"
                )
            if record.period not in policy.periods:
                raise ValidationFailed(
                    f"records[{index}] 报告期 {record.period} 不在规则声明的期间内"
                )
            if record.source_id is not None and record.source_id not in policy.source_priority:
                raise ValidationFailed(
                    f"records[{index}] 来源 {record.source_id} 未在 source_priority 中登记"
                )
            seen_ids.add(record.record_id)
            parsed.append((dict(raw), record, content_digest([dict(sorted(raw.items()))])))

        request_digest = content_digest([raw for raw, _, _ in parsed])
        with transaction(self.db, immediate=True):
            # 样本身份在同一冻结范围内只能归属唯一报送主体，跨主体冲突立即拒绝。
            for _, record, _ in parsed:
                owner = self.db.execute(
                    "SELECT reporter_id FROM quality_records "
                    "WHERE policy_id=? AND policy_version=? AND sample_id=? LIMIT 1",
                    (policy_id, version, record.sample_id),
                ).fetchone()
                if owner is not None and owner["reporter_id"] != record.reporter_id:
                    raise Conflict(
                        f"样本 {record.sample_id} 已归属报送主体 {owner['reporter_id']}，"
                        f"不能再由 {record.reporter_id} 报送"
                    )

            inserted = 0
            replayed = 0
            for raw, record, digest in parsed:
                existing = self.db.execute(
                    "SELECT content_sha256 FROM quality_records "
                    "WHERE policy_id=? AND policy_version=? AND record_id=?",
                    (policy_id, version, record.record_id),
                ).fetchone()
                if existing is not None:
                    if existing["content_sha256"] != digest:
                        raise Conflict(
                            f"记录 {record.record_id} 已存在且内容不同，原始报送不能被覆盖"
                        )
                    replayed += 1
                    continue
                self.db.execute(
                    "INSERT INTO quality_records(record_id,policy_id,policy_version,reporter_id,sample_id,"
                    "indicator_id,period,kind,value,source_id,recorded_at,content_sha256,imported_by,imported_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (record.record_id, policy_id, version, record.reporter_id, record.sample_id,
                     record.indicator_id, record.period, record.kind,
                     None if record.value is None else format(record.value, "f"),
                     record.source_id, record.recorded_at, digest, actor.user_id, self._now()),
                )
                inserted += 1
            self._audit(
                "policy", f"{policy_id}@{version}", "records.imported", actor.user_id,
                {"inserted": inserted, "replayed": replayed, "request_sha256": request_digest,
                 "idempotency_key": idempotency_key},
            )
        return {"policy_id": policy_id, "version": version, "inserted": inserted, "replayed": replayed,
                "request_sha256": request_digest}

    def revoke_record(
        self,
        token: str,
        record_id: str,
        reason: str,
        policy_id: str | None = None,
        version: int | None = None,
    ) -> dict[str, Any]:
        """登记一条撤销记录（不删除原始报送，审计要求可追溯）。"""

        actor = self.auth.require(token, "record.revoke")
        if not reason.strip():
            raise ValidationFailed("撤销原因不能为空")
        with transaction(self.db, immediate=True):
            if policy_id is not None and version is not None:
                original = self.db.execute(
                    "SELECT * FROM quality_records WHERE policy_id=? AND policy_version=? AND record_id=?",
                    (policy_id, version, record_id),
                ).fetchone()
            else:
                original = self.db.execute(
                    "SELECT * FROM quality_records WHERE record_id=? "
                    "ORDER BY policy_version DESC LIMIT 1", (record_id,)
                ).fetchone()
            if original is None:
                scope = f"（{policy_id}@{version}）" if policy_id else ""
                raise NotFound(f"报送记录不存在{scope}: {record_id}")
            if original["kind"] == "withdrawal":
                raise InvalidState("该记录本身就是撤销记录，不能再次撤销")
            withdrawal_id = f"withdrawal-of-{record_id}"
            if self.db.execute(
                "SELECT 1 FROM quality_records WHERE policy_id=? AND policy_version=? AND record_id=?",
                (original["policy_id"], original["policy_version"], withdrawal_id),
            ).fetchone():
                raise Conflict(f"记录 {record_id} 已被撤销")
            self.db.execute(
                "INSERT INTO quality_records(record_id,policy_id,policy_version,reporter_id,sample_id,"
                "indicator_id,period,kind,value,source_id,recorded_at,content_sha256,imported_by,imported_at) "
                "VALUES(?,?,?,?,?,?,?, 'withdrawal', NULL, NULL, ?,?,?,?)",
                (withdrawal_id, original["policy_id"], original["policy_version"], original["reporter_id"],
                 original["sample_id"], original["indicator_id"], original["period"], self._now(),
                 content_digest([{"withdrawal_of": record_id, "reason": reason}]),
                 actor.user_id, self._now()),
            )
            self._audit(
                "record", record_id, "record.revoked", actor.user_id,
                {"withdrawal_id": withdrawal_id, "reason": reason.strip()},
            )
        return {"record_id": record_id, "withdrawal_id": withdrawal_id, "status": "withdrawn"}

    def _records_for_policy(self, policy_id: str, version: int) -> list[ObservationRecord]:
        rows = self.db.execute(
            "SELECT * FROM quality_records WHERE policy_id=? AND policy_version=? ORDER BY record_id",
            (policy_id, version),
        ).fetchall()
        return [
            ObservationRecord(
                reporter_id=row["reporter_id"], sample_id=row["sample_id"],
                indicator_id=row["indicator_id"], period=row["period"], kind=row["kind"],
                recorded_at=row["recorded_at"],
                value=None if row["value"] is None else Decimal(row["value"]),
                source_id=row["source_id"], record_id=row["record_id"],
            )
            for row in rows
        ]

    # -- 质量分析（新模型，不可变版本） -----------------------------------

    def run_analysis(self, token: str, policy_id: str, version: int) -> dict[str, Any]:
        actor = self.auth.require(token, "analysis.run")
        policy = self._load_policy(policy_id, version)
        records = self._records_for_policy(policy_id, version)
        if not records:
            raise InvalidState(f"规则 {policy_id}@{version} 下还没有任何报送记录，无法冻结分析")
        try:
            result = evaluate(policy, records)
        except QualityInputError as exc:
            raise ValidationFailed(str(exc)) from exc
        input_digest = content_digest([field_canonical_tuple(item) for item in records])
        with transaction(self.db, immediate=True):
            existing = self.db.execute(
                "SELECT analysis_id FROM analyses "
                "WHERE policy_id=? AND policy_version=? AND input_sha256=?",
                (policy_id, version, input_digest),
            ).fetchone()
            if existing is not None:
                analysis_id = existing["analysis_id"]
            else:
                cursor = self.db.execute(
                    "INSERT INTO analyses(policy_id,policy_version,input_sha256,algorithm_version,"
                    "result_json,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (policy_id, version, input_digest, ALGORITHM_VERSION,
                     canonical_json(result), actor.user_id, self._now()),
                )
                analysis_id = cursor.lastrowid
                self._audit(
                    "analysis", str(analysis_id), "analysis.created", actor.user_id,
                    {"policy_id": policy_id, "policy_version": version, "input_sha256": input_digest},
                )
        return self._analysis_view(analysis_id)

    def _analysis_view(self, analysis_id: int) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM analyses WHERE analysis_id=?", (analysis_id,)).fetchone()
        if row is None:
            raise NotFound(f"分析版本不存在: {analysis_id}")
        result = json.loads(row["result_json"])
        referenced = {
            rid
            for sample in result.get("samples", [])
            for period in sample.get("periods", [])
            for rid in (period.get("record_ids") or [])
        }
        raw_records: list[dict[str, Any]] = []
        if referenced:
            placeholders = ",".join("?" for _ in referenced)
            raw_records = [
                dict(raw) for raw in self.db.execute(
                    "SELECT record_id,reporter_id,sample_id,indicator_id,period,kind,value,source_id,recorded_at "
                    f"FROM quality_records WHERE policy_id=? AND policy_version=? "
                    f"AND record_id IN ({placeholders}) ORDER BY record_id",
                    (row["policy_id"], row["policy_version"], *sorted(referenced)),
                ).fetchall()
            ]
        decision = self.db.execute(
            "SELECT decision_id,decision,reason,decided_by,decided_at,status,revoked_by,revoked_at,revoke_reason "
            "FROM decisions WHERE analysis_id=?", (analysis_id,)
        ).fetchone()
        return {
            "analysis_id": analysis_id,
            "policy_id": row["policy_id"],
            "policy_version": row["policy_version"],
            "input_sha256": row["input_sha256"],
            "algorithm_version": row["algorithm_version"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "decision": None if decision is None else dict(decision),
            "result": result,
            "records": raw_records,
        }

    def get_analysis(self, token: str, analysis_id: int) -> dict[str, Any]:
        self.auth.require(token, "read")
        return self._analysis_view(analysis_id)

    def list_analyses(self, token: str, policy_id: str) -> list[dict[str, Any]]:
        self.auth.require(token, "read")
        rows = self.db.execute(
            "SELECT a.analysis_id,a.policy_id,a.policy_version,a.input_sha256,a.algorithm_version,"
            "a.created_at, d.status AS decision_status "
            "FROM analyses a LEFT JOIN decisions d ON d.analysis_id=a.analysis_id "
            "WHERE a.policy_id=? ORDER BY a.policy_version, a.analysis_id",
            (policy_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # -- 发布决定（不可静默修改） -----------------------------------------

    def decide(self, token: str, analysis_id: int, decision: str, reason: str) -> dict[str, Any]:
        actor = self.auth.require(token, "decision.write")
        if decision not in {"release", "hold", "reject"}:
            raise ValidationFailed("decision 只能是 release、hold 或 reject")
        if not reason.strip():
            raise ValidationFailed("决定原因不能为空")
        with transaction(self.db, immediate=True):
            analysis = self.db.execute(
                "SELECT * FROM analyses WHERE analysis_id=?", (analysis_id,)
            ).fetchone()
            if analysis is None:
                raise NotFound(f"分析版本不存在: {analysis_id}")
            if analysis["created_by"] == actor.user_id:
                raise Forbidden("分析员不能批准自己运行的分析版本")
            existing = self.db.execute(
                "SELECT decision_id,status FROM decisions WHERE analysis_id=?", (analysis_id,)
            ).fetchone()
            if existing is not None:
                if existing["status"] == "active":
                    raise InvalidState("该分析版本已有生效决定，不能静默修改；请先撤销后基于后继分析版本重新决定")
                raise InvalidState("该分析版本的决定已撤销，不能在旧版本上重新决定；请运行后继分析版本")
            cursor = self.db.execute(
                "INSERT INTO decisions(analysis_id,decision,reason,decided_by,decided_at) VALUES(?,?,?,?,?)",
                (analysis_id, decision, reason.strip(), actor.user_id, self._now()),
            )
            self._audit(
                "decision", str(cursor.lastrowid), "decision.recorded", actor.user_id,
                {"analysis_id": analysis_id, "policy_id": analysis["policy_id"],
                 "policy_version": analysis["policy_version"], "decision": decision},
            )
        return self._analysis_view(analysis_id)

    def revoke_decision(self, token: str, analysis_id: int, reason: str) -> dict[str, Any]:
        actor = self.auth.require(token, "decision.revoke")
        if not reason.strip():
            raise ValidationFailed("撤销原因不能为空")
        with transaction(self.db, immediate=True):
            row = self.db.execute(
                "SELECT decision_id,status FROM decisions WHERE analysis_id=?", (analysis_id,)
            ).fetchone()
            if row is None:
                raise NotFound("该分析版本没有可撤销的决定")
            if row["status"] != "active":
                raise InvalidState("决定已经撤销，不能重复撤销")
            self.db.execute(
                "UPDATE decisions SET status='revoked',revoked_by=?,revoked_at=?,revoke_reason=? "
                "WHERE decision_id=? AND status='active'",
                (actor.user_id, self._now(), reason.strip(), row["decision_id"]),
            )
            self._audit(
                "decision", str(row["decision_id"]), "decision.revoked", actor.user_id,
                {"analysis_id": analysis_id, "reason": reason.strip()},
            )
        return self._analysis_view(analysis_id)

    # -- 旧算法历史（原样保留） -------------------------------------------

    def register_legacy_analysis(
        self, token: str, lot_id: str, input_summary: Mapping[str, Any], result: Mapping[str, Any]
    ) -> dict[str, Any]:
        """登记旧算法（按观测点计合格率）产出，保证历史决定可复核。"""

        actor = self.auth.require(token, "legacy.register")
        if not lot_id.strip():
            raise ValidationFailed("lot_id 不能为空")
        if not isinstance(input_summary, Mapping) or not input_summary:
            raise ValidationFailed("旧分析输入摘要不能为空")
        if not isinstance(result, Mapping) or not result:
            raise ValidationFailed("旧分析结果不能为空")
        summary_text = canonical_json(input_summary)
        result_text = canonical_json(result)
        with transaction(self.db, immediate=True):
            existing = self.db.execute(
                "SELECT legacy_id FROM legacy_analyses "
                "WHERE lot_id=? AND algorithm_version=? AND input_summary_json=?",
                (lot_id, LEGACY_ALGORITHM_VERSION, summary_text),
            ).fetchone()
            if existing is not None:
                legacy_id = existing["legacy_id"]
                replayed = True
            else:
                cursor = self.db.execute(
                    "INSERT INTO legacy_analyses(lot_id,algorithm_version,input_summary_json,result_json,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (lot_id, LEGACY_ALGORITHM_VERSION, summary_text, result_text,
                     actor.user_id, self._now()),
                )
                legacy_id = cursor.lastrowid
                replayed = False
                self._audit(
                    "legacy_analysis", str(legacy_id), "legacy.registered", actor.user_id,
                    {"lot_id": lot_id, "algorithm_version": LEGACY_ALGORITHM_VERSION},
                )
        return self.get_legacy_analysis(token, legacy_id) | {"replayed": replayed}

    def get_legacy_analysis(self, token: str, legacy_id: int) -> dict[str, Any]:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM legacy_analyses WHERE legacy_id=?", (legacy_id,)).fetchone()
        if row is None:
            raise NotFound(f"旧分析版本不存在: {legacy_id}")
        return {
            "legacy_id": row["legacy_id"],
            "lot_id": row["lot_id"],
            "algorithm_version": row["algorithm_version"],
            "input_summary": json.loads(row["input_summary_json"]),
            "result": json.loads(row["result_json"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    def list_legacy_analyses(self, token: str, lot_id: str) -> list[dict[str, Any]]:
        self.auth.require(token, "read")
        rows = self.db.execute(
            "SELECT legacy_id,lot_id,algorithm_version,created_at FROM legacy_analyses "
            "WHERE lot_id=? ORDER BY legacy_id", (lot_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    # -- 审计 -------------------------------------------------------------

    def audit(
        self, token: str, entity_type: str | None = None, entity_id: str | None = None
    ) -> list[dict[str, Any]]:
        self.auth.require(token, "audit.read")
        sql = "SELECT * FROM quality_events"
        clauses: list[str] = []
        params: list[Any] = []
        if entity_type is not None:
            clauses.append("entity_type=?")
            params.append(entity_type)
        if entity_id is not None:
            clauses.append("entity_id=?")
            params.append(entity_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY event_id"
        rows = self.db.execute(sql, params).fetchall()
        return [dict(row) | {"payload": json.loads(row["payload_json"])} for row in rows]
