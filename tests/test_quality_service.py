from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from metric_quality.clock import FrozenClock
from metric_quality.errors import (
    Conflict,
    Forbidden,
    InvalidState,
    NotFound,
    ValidationFailed,
)
from metric_quality.service import MetricQualityService

POLICY = {
    "policy_id": "q",
    "version": 1,
    "indicator_id": "idx",
    "reporter_ids": ["A", "B"],
    "periods": ["2025", "2026"],
    "source_priority": ["official", "gateway"],
    "operator": "gte",
    "threshold": "0.8",
    "conflict_tolerance": "0.02",
}


def row(record_id, reporter, sample, period, kind="report", *, at, value="0.9", source="official", **extra):
    item = {
        "record_id": record_id, "reporter_id": reporter, "sample_id": sample,
        "indicator_id": "idx", "period": period, "kind": kind, "recorded_at": at,
    }
    if value is not None:
        item["value"] = value
    if source is not None:
        item["source_id"] = source
    item.update(extra)
    return item


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FrozenClock(datetime(2026, 9, 1, tzinfo=timezone.utc))
        self.service = MetricQualityService(":memory:", clock=self.clock)
        self.service.bootstrap_admin()
        self.admin = self.service.auth.login("admin", "metric-admin")
        for uid, role in (
            ("rep", "reporter"), ("ana", "analyst"), ("app", "approver"), ("aud", "auditor"),
        ):
            self.service.auth.create_user(uid, f"{uid}-password", role)
        self.reporter = self.service.auth.login("rep", "rep-password")
        self.analyst = self.service.auth.login("ana", "ana-password")
        self.approver = self.service.auth.login("app", "app-password")
        self.auditor = self.service.auth.login("aud", "aud-password")
        self.service.publish_policy(self.admin, POLICY)

    def _seed(self, records) -> dict:
        return self.service.import_records(self.reporter, "q", 1, records)


class WorkflowTests(ServiceTestBase):
    def test_full_drilldown_from_rate_to_sample_to_records(self) -> None:
        self._seed([
            row("a1-1", "A", "s1", "2025", at="2026-01-01T00:00:00+00:00"),
            row("a1-2", "A", "s1", "2026", at="2026-02-01T00:00:00+00:00"),
            row("b1-1", "B", "s2", "2025", source="gateway", at="2026-03-01T00:00:00+00:00"),
            # s2 缺 2026 期 → 证据不足
        ])
        analysis = self.service.run_analysis(self.analyst, "q", 1)
        result = analysis["result"]
        self.assertEqual(result["sample_count"], 2)
        self.assertEqual(result["counts"], {"pass": 1, "reject": 0, "insufficient": 1})
        # 通过率 -> 样本
        s1 = next(s for s in result["samples"] if s["sample_id"] == "s1")
        self.assertEqual(s1["conclusion"], "pass")
        # 样本结论 -> 各期引用的原始记录 id
        period_ids = {rid for p in s1["periods"] for rid in p["record_ids"]}
        # 分析视图携带全部被引用的原始记录，供下钻核验
        raw_ids = {r["record_id"] for r in analysis["records"]}
        self.assertEqual(raw_ids, {"a1-1", "a1-2", "b1-1"})
        self.assertEqual(period_ids, {"a1-1", "a1-2"})

    def test_over_100_percent_bug_is_fixed(self) -> None:
        self._seed([
            row("s1-1", "A", "s1", "2025", value="0.95", at="2026-01-01T00:00:00+00:00"),
            row("s1-2", "A", "s1", "2026", value="0.95", at="2026-02-01T00:00:00+00:00"),
        ])
        result = self.service.run_analysis(self.analyst, "q", 1)["result"]
        self.assertEqual(result["rates"]["pass"], "1.000000")
        self.assertEqual(result["sample_count"], 1)


class RecordHandlingTests(ServiceTestBase):
    def test_resubmission_missing_withdrawal_have_determined_outcomes(self) -> None:
        self._seed([
            row("s1-1", "A", "s1", "2025", at="2026-01-01T00:00:00+00:00"),
            row("s1-2", "A", "s1", "2026", value="0.5", at="2026-02-01T00:00:00+00:00"),
            row("s1-3", "A", "s1", "2026", kind="resubmission", value="0.9", at="2026-02-05T00:00:00+00:00"),
            row("s2-1", "A", "s2", "2025", at="2026-03-01T00:00:00+00:00"),
            row("s2-2", "A", "s2", "2026", value="0.95", at="2026-04-01T00:00:00+00:00"),
            row("s2-3", "A", "s2", "2026", kind="missing", value=None, source=None, at="2026-05-01T00:00:00+00:00"),
        ])
        self.service.revoke_record(self.reporter, "s1-1", "口径更正，撤销")
        # s1 的 2025 期被撤销 → insufficient；s2 被后到的缺期标记置为缺期 → insufficient
        samples = {
            s["sample_id"]: s
            for s in self.service.run_analysis(self.analyst, "q", 1)["result"]["samples"]
        }
        self.assertEqual(samples["s1"]["conclusion"], "insufficient")
        self.assertEqual(samples["s1"]["periods"][0]["status"], "withdrawn")
        self.assertEqual(samples["s2"]["periods"][1]["status"], "missing")

    def test_revoke_does_not_delete_original(self) -> None:
        self._seed([row("r1", "A", "s1", "2025", at="2026-01-01T00:00:00+00:00")])
        self.service.revoke_record(self.reporter, "r1", "撤销测试")
        rows = self.service.db.execute(
            "SELECT kind FROM quality_records WHERE record_id IN ('r1','withdrawal-of-r1') ORDER BY record_id"
        ).fetchall()
        self.assertEqual([r["kind"] for r in rows], ["report", "withdrawal"])

    def test_double_revoke_conflicts(self) -> None:
        self._seed([row("r1", "A", "s1", "2025", at="2026-01-01T00:00:00+00:00")])
        self.service.revoke_record(self.reporter, "r1", "第一次撤销")
        with self.assertRaises(Conflict):
            self.service.revoke_record(self.reporter, "r1", "第二次撤销")

    def test_conflicting_sources_are_insufficient_not_termination(self) -> None:
        # 旧行为：数量不一致直接终止；新行为：冲突期确定性地判为证据不足。
        self._seed([
            row("s1-1", "A", "s1", "2025", value="0.95", at="2026-01-01T00:00:00+00:00"),
            row("s1-1g", "A", "s1", "2025", value="0.70", source="gateway", at="2026-01-02T00:00:00+00:00"),
            row("s1-2", "A", "s1", "2026", value="0.9", at="2026-02-01T00:00:00+00:00"),
        ])
        sample = self.service.run_analysis(self.analyst, "q", 1)["result"]["samples"][0]
        self.assertEqual(sample["conclusion"], "insufficient")

    def test_reimport_same_records_is_idempotent(self) -> None:
        records = [row("r1", "A", "s1", "2025", at="2026-01-01T00:00:00+00:00")]
        first = self._seed(records)
        second = self.service.import_records(self.reporter, "q", 1, records)
        self.assertEqual(first["inserted"], 1)
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["replayed"], 1)

    def test_reimport_with_changed_value_conflicts(self) -> None:
        self._seed([row("r1", "A", "s1", "2025", value="0.9", at="2026-01-01T00:00:00+00:00")])
        with self.assertRaises(Conflict):
            self._seed([row("r1", "A", "s1", "2025", value="0.99", at="2026-01-01T00:00:00+00:00")])

    def test_sample_identity_cannot_cross_reporters(self) -> None:
        self._seed([row("r1", "A", "s1", "2025", at="2026-01-01T00:00:00+00:00")])
        with self.assertRaises(Conflict):
            self._seed([row("r2", "B", "s1", "2025", source="gateway", at="2026-02-01T00:00:00+00:00")])


class ValidationTests(ServiceTestBase):
    def test_unknown_period_is_business_error(self) -> None:
        with self.assertRaises(ValidationFailed) as ctx:
            self._seed([row("r1", "A", "s1", "1999", at="2026-01-01T00:00:00+00:00")])
        self.assertIn("报告期", str(ctx.exception))

    def test_unknown_source_is_business_error(self) -> None:
        with self.assertRaises(ValidationFailed):
            self._seed([row("r1", "A", "s1", "2025", source="rogue", at="2026-01-01T00:00:00+00:00")])

    def test_missing_record_id_is_business_error(self) -> None:
        bad = {"reporter_id": "A", "sample_id": "s1", "indicator_id": "idx",
               "period": "2025", "kind": "report", "value": "0.9",
               "source_id": "official", "recorded_at": "2026-01-01T00:00:00+00:00"}
        with self.assertRaises(ValidationFailed):
            self._seed([bad])

    def test_empty_batch_rejected(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.import_records(self.reporter, "q", 1, [])

    def test_analysis_without_records_is_invalid_state(self) -> None:
        with self.assertRaises(InvalidState):
            self.service.run_analysis(self.analyst, "q", 1)

    def test_policy_superseding_missing_version_rejected(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.publish_policy(self.admin, {**POLICY, "version": 3, "supersedes_version": 2})


class VersioningAndDecisionTests(ServiceTestBase):
    def _passing_records(self):
        return [
            row("s1-1", "A", "s1", "2025", at="2026-01-01T00:00:00+00:00"),
            row("s1-2", "A", "s1", "2026", at="2026-02-01T00:00:00+00:00"),
        ]

    def test_same_input_returns_same_analysis_version(self) -> None:
        self._seed(self._passing_records())
        first = self.service.run_analysis(self.analyst, "q", 1)
        second = self.service.run_analysis(self.analyst, "q", 1)
        self.assertEqual(first["analysis_id"], second["analysis_id"])

    def test_published_decision_cannot_be_silently_changed(self) -> None:
        self._seed(self._passing_records())
        analysis = self.service.run_analysis(self.analyst, "q", 1)
        self.service.decide(self.approver, analysis["analysis_id"], "hold", "等待复核")
        with self.assertRaises(InvalidState):
            self.service.decide(self.approver, analysis["analysis_id"], "release", "改为放行")
        stored = self.service.get_analysis(self.auditor, analysis["analysis_id"])
        self.assertEqual(stored["decision"]["decision"], "hold")
        self.assertEqual(stored["decision"]["status"], "active")

    def test_analyst_cannot_approve_own_analysis(self) -> None:
        # 同时具备分析与审批权限的账号也不能批准自己运行的分析（职责分离）
        self._seed(self._passing_records())
        analysis = self.service.run_analysis(self.admin, "q", 1)
        with self.assertRaises(Forbidden):
            self.service.decide(self.admin, analysis["analysis_id"], "release", "自我批准")
        # 另一名审批人可以正常批准
        ok = self.service.decide(self.approver, analysis["analysis_id"], "release", "独立复核通过")
        self.assertEqual(ok["decision"]["status"], "active")

    def test_revoke_then_new_decision_must_use_successor(self) -> None:
        self._seed(self._passing_records())
        v1 = self.service.run_analysis(self.analyst, "q", 1)
        self.service.decide(self.approver, v1["analysis_id"], "hold", "暂缓")
        self.service.revoke_decision(self.approver, v1["analysis_id"], "换规则重评")
        # 已撤销版本上不能再做决定
        with self.assertRaises(InvalidState):
            self.service.decide(self.approver, v1["analysis_id"], "release", "旧版本重做")
        # 后继规则版本 + 后继分析
        self.service.publish_policy(self.admin, {
            **POLICY, "version": 2, "threshold": "0.7", "supersedes_version": 1,
        })
        self.service.import_records(self.reporter, "q", 2, self._passing_records())
        v2 = self.service.run_analysis(self.analyst, "q", 2)
        decided = self.service.decide(self.approver, v2["analysis_id"], "release", "按新规则放行")
        self.assertEqual(decided["decision"]["decision"], "release")
        # v1 的决定仍保留撤销痕迹，没有被静默改写
        old = self.service.get_analysis(self.auditor, v1["analysis_id"])
        self.assertEqual(old["decision"]["status"], "revoked")
        self.assertEqual(old["decision"]["decision"], "hold")

    def test_policy_version_is_append_only(self) -> None:
        # 相同版本号不能再次发布（即使内容不同）
        with self.assertRaises(Conflict):
            self.service.publish_policy(self.admin, {**POLICY, "threshold": "0.5"})
        chain = self.service.list_policies(self.auditor, "q")
        self.assertEqual([p["version"] for p in chain], [1])

    def test_legacy_analysis_is_preserved_untouched(self) -> None:
        summary = {"sample_count": 4, "measurement_count": 13}
        result = {"yield": {"yield": 1.08}}
        stored = self.service.register_legacy_analysis(
            self.analyst, "LOT-1", summary, result
        )
        self.assertEqual(stored["algorithm_version"], "metric-quality-observation-yield/1")
        self.assertEqual(stored["result"], result)
        self.assertEqual(stored["input_summary"], summary)
        # 重复登记同 lot+输入摘要：幂等，不产生新版本也不改写
        again = self.service.register_legacy_analysis(self.analyst, "LOT-1", summary, result)
        self.assertTrue(again["replayed"])
        self.assertEqual(again["legacy_id"], stored["legacy_id"])

    def test_legacy_digest_change_creates_separate_version(self) -> None:
        first = self.service.register_legacy_analysis(
            self.analyst, "LOT-1", {"sample_count": 4}, {"yield": {"yield": 1.08}}
        )
        second = self.service.register_legacy_analysis(
            self.analyst, "LOT-1", {"sample_count": 5}, {"yield": {"yield": 0.9}}
        )
        self.assertNotEqual(first["legacy_id"], second["legacy_id"])


class PermissionTests(ServiceTestBase):
    def test_reporter_cannot_run_analysis(self) -> None:
        with self.assertRaises(PermissionError):
            self.service.run_analysis(self.reporter, "q", 1)

    def test_analyst_cannot_publish_policy(self) -> None:
        with self.assertRaises(PermissionError):
            self.service.publish_policy(self.analyst, {**POLICY, "version": 2})

    def test_auditor_reads_audit_trail(self) -> None:
        self._seed([row("r1", "A", "s1", "2025", at="2026-01-01T00:00:00+00:00")])
        events = self.service.audit(self.auditor, "policy", "q@1")
        types = [e["event_type"] for e in events]
        self.assertIn("policy.published", types)
        self.assertIn("records.imported", types)

    def test_reporter_cannot_read_audit(self) -> None:
        with self.assertRaises(PermissionError):
            self.service.audit(self.reporter)


if __name__ == "__main__":
    unittest.main()
