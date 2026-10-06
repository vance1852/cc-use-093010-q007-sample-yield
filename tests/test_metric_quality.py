from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from metric_quality.acceptance import run as acceptance_run
from metric_quality.analytics import ALGORITHM_VERSION
from metric_quality.api import JsonApplication
from metric_quality.clock import FrozenClock
from metric_quality.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from metric_quality.service import MetricQualityService


RULE_SET_V1 = {
    "rule_set_id": "gdti-quarterly",
    "version": 1,
    "title": "全球数字贸易指数季度质量规则",
    "indicator": {"key": "digital_trade_index", "label": "数字贸易指数", "direction": "higher", "threshold": "0.8"},
    "expected_periods": ["2026-Q1", "2026-Q2", "2026-Q3"],
    "freeze_rules": {
        "min_periods": 3,
        "max_failed_periods": 0,
        "source_priority": ["office-a", "office-b", "secretariat"],
    },
}


def report_row(source: str, sample: str, period: str, revision: int, value: object) -> dict:
    return {
        "source_id": source,
        "sample_id": sample,
        "period": period,
        "revision": revision,
        "kind": "report",
        "value": value,
    }


def retraction_row(source: str, sample: str, period: str, revision: int) -> dict:
    return {
        "source_id": source,
        "sample_id": sample,
        "period": period,
        "revision": revision,
        "kind": "retraction",
        "value": None,
    }


def sample(result: dict, sample_id: str) -> dict:
    return next(item for item in result["samples"] if item["sample_id"] == sample_id)


class MetricQualityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        self.service = MetricQualityService(self.connection, self.clock)
        for user_id, role in (("op", "operator"), ("analyst", "analyst"), ("approver", "approver"), ("auditor", "auditor")):
            self.service.create_user(user_id, user_id, role)
        for source_id in ("office-a", "office-b", "secretariat"):
            self.service.register_source("op", source_id, source_id, "统计局")
        self.service.publish_rule_set("analyst", RULE_SET_V1)
        self.service.create_batch("op", "batch-1", "gdti-quarterly", 1, ["economy-a", "economy-b"])

    def tearDown(self) -> None:
        self.connection.close()

    def _rows_full_pass(self) -> list[dict]:
        rows = [
            report_row("office-a", "economy-a", period, 1, value)
            for period, value in (("2026-Q1", "0.84"), ("2026-Q2", "0.91"), ("2026-Q3", "0.87"))
        ]
        rows += [
            report_row("office-b", "economy-b", period, 1, value)
            for period, value in (("2026-Q1", "0.86"), ("2026-Q2", "0.90"), ("2026-Q3", "0.83"))
        ]
        return rows

    def _analyze(self, rows: list[dict], key: str = "key-1") -> dict:
        self.service.import_observations("op", "batch-1", key, rows)
        self.service.freeze_batch("analyst", "batch-1", 1)
        return self.service.run_analysis("analyst", "batch-1")

    def test_rates_are_computed_over_samples_not_observation_points(self) -> None:
        rows = self._rows_full_pass()
        rows += [
            report_row("office-b", "economy-b", "2026-Q1", 2, "0.88"),
            report_row("secretariat", "economy-b", "2026-Q1", 1, "0.84"),
        ]
        result = self._analyze(rows)["result"]
        self.assertEqual(result["sample_count"], 2)
        self.assertEqual(sum(result["conclusions"].values()), 2)
        self.assertEqual(result["conclusions"], {"pass": 2, "reject": 0, "insufficient_evidence": 0})
        self.assertEqual(result["rates"]["pass"], "1.000000")
        for rate in result["rates"].values():
            self.assertLessEqual(Decimal(rate), Decimal(1))

    def test_missing_period_means_insufficient_evidence(self) -> None:
        rows = self._rows_full_pass()[:3]
        rows += [
            report_row("office-b", "economy-b", "2026-Q1", 1, "0.85"),
            report_row("office-b", "economy-b", "2026-Q2", 1, "0.86"),
        ]
        result = self._analyze(rows)["result"]
        self.assertEqual(result["conclusions"], {"pass": 1, "reject": 0, "insufficient_evidence": 1})
        self.assertEqual(
            result["rates"],
            {"pass": "0.500000", "reject": "0.000000", "insufficient_evidence": "0.500000"},
        )
        economy_b = sample(result, "economy-b")
        self.assertEqual(economy_b["conclusion"], "insufficient_evidence")
        self.assertEqual(economy_b["missing_periods"], ["2026-Q3"])

    def test_failed_period_rejects_sample(self) -> None:
        rows = self._rows_full_pass()[:3]
        rows += [
            report_row("office-b", "economy-b", "2026-Q1", 1, "0.85"),
            report_row("office-b", "economy-b", "2026-Q2", 1, "0.40"),
            report_row("office-b", "economy-b", "2026-Q3", 1, "0.87"),
        ]
        result = self._analyze(rows)["result"]
        self.assertEqual(result["conclusions"]["reject"], 1)
        self.assertEqual(sample(result, "economy-b")["failed_periods"], ["2026-Q2"])

    def test_rereport_supersedes_earlier_revision_and_keeps_history(self) -> None:
        rows = self._rows_full_pass()[:3]
        rows += [
            report_row("office-b", "economy-b", "2026-Q1", 1, "0.50"),
            report_row("office-b", "economy-b", "2026-Q1", 2, "0.95"),
            report_row("office-b", "economy-b", "2026-Q2", 1, "0.86"),
            report_row("office-b", "economy-b", "2026-Q3", 1, "0.87"),
        ]
        result = self._analyze(rows)["result"]
        economy_b = sample(result, "economy-b")
        self.assertEqual(economy_b["conclusion"], "pass")
        first_quarter = next(item for item in economy_b["periods"] if item["period"] == "2026-Q1")
        self.assertEqual(first_quarter["value"], "0.95")
        self.assertEqual(first_quarter["revision"], 2)
        self.assertEqual(economy_b["superseded_records"], 1)
        count = self.connection.execute("SELECT count(*) FROM observation_records").fetchone()[0]
        self.assertEqual(count, 7)

    def test_rereport_revision_must_be_sequential(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.import_observations("op", "batch-1", "key-x", [report_row("office-b", "economy-b", "2026-Q1", 2, "0.9")])
        self.service.import_observations("op", "batch-1", "key-1", [report_row("office-b", "economy-b", "2026-Q1", 1, "0.5")])
        self.service.import_observations("op", "batch-1", "key-2", [report_row("office-b", "economy-b", "2026-Q1", 2, "0.9")])
        with self.assertRaises(ValidationFailed):
            self.service.import_observations("op", "batch-1", "key-3", [report_row("office-b", "economy-b", "2026-Q1", 2, "0.99")])

    def test_retraction_removes_source_contribution(self) -> None:
        rows = self._rows_full_pass()[:3]
        rows += [
            report_row("office-b", "economy-b", "2026-Q1", 1, "0.85"),
            report_row("office-b", "economy-b", "2026-Q2", 1, "0.86"),
            report_row("office-b", "economy-b", "2026-Q3", 1, "0.87"),
            retraction_row("office-b", "economy-b", "2026-Q3", 2),
        ]
        result = self._analyze(rows)["result"]
        economy_b = sample(result, "economy-b")
        self.assertEqual(economy_b["conclusion"], "insufficient_evidence")
        self.assertEqual(economy_b["missing_periods"], ["2026-Q3"])
        self.assertEqual(economy_b["retracted_records"], 1)

    def test_retraction_falls_back_to_lower_priority_source(self) -> None:
        rows = self._rows_full_pass()[:3]
        rows += [
            report_row("office-b", "economy-b", "2026-Q1", 1, "0.85"),
            report_row("office-b", "economy-b", "2026-Q2", 1, "0.86"),
            report_row("office-b", "economy-b", "2026-Q3", 1, "0.40"),
            retraction_row("office-b", "economy-b", "2026-Q3", 2),
            report_row("secretariat", "economy-b", "2026-Q3", 1, "0.88"),
        ]
        result = self._analyze(rows)["result"]
        economy_b = sample(result, "economy-b")
        self.assertEqual(economy_b["conclusion"], "pass")
        third_quarter = next(item for item in economy_b["periods"] if item["period"] == "2026-Q3")
        self.assertEqual(third_quarter["source_id"], "secretariat")
        self.assertEqual(third_quarter["value"], "0.88")

    def test_conflicting_sources_resolve_by_priority(self) -> None:
        rows = self._rows_full_pass()[:3]
        rows += [
            report_row("office-b", "economy-b", "2026-Q1", 1, "0.85"),
            report_row("secretariat", "economy-b", "2026-Q1", 1, "0.60"),
            report_row("office-b", "economy-b", "2026-Q2", 1, "0.86"),
            report_row("office-b", "economy-b", "2026-Q3", 1, "0.87"),
        ]
        analysis = self._analyze(rows)
        result = analysis["result"]
        economy_b = sample(result, "economy-b")
        self.assertEqual(economy_b["conclusion"], "pass")
        self.assertEqual(len(economy_b["conflicts"]), 1)
        conflict = economy_b["conflicts"][0]
        self.assertEqual(conflict["period"], "2026-Q1")
        self.assertEqual(conflict["chosen"], {"source_id": "office-b", "value": "0.85"})
        self.assertEqual(conflict["discarded"], [{"source_id": "secretariat", "value": "0.60"}])
        replay = self.service.run_analysis("analyst", "batch-1")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["result"], result)

    def test_same_value_from_two_sources_is_not_a_conflict(self) -> None:
        rows = self._rows_full_pass()
        rows += [
            report_row("secretariat", "economy-b", period, 1, value)
            for period, value in (("2026-Q1", "0.86"), ("2026-Q2", "0.90"), ("2026-Q3", "0.83"))
        ]
        result = self._analyze(rows)["result"]
        self.assertEqual(sample(result, "economy-b")["conflicts"], [])

    def test_successor_analysis_preserves_previous_version(self) -> None:
        first = self._analyze(self._rows_full_pass())
        stricter = dict(RULE_SET_V1, version=2, indicator=dict(RULE_SET_V1["indicator"], threshold="0.95"))
        self.service.publish_rule_set("analyst", stricter)
        second = self.service.run_analysis("analyst", "batch-1", rule_set_version=2)
        self.assertNotEqual(first["analysis_id"], second["analysis_id"])
        self.assertEqual(second["supersedes_analysis_id"], first["analysis_id"])
        self.assertFalse(second["replayed"])
        stored_first = self.service.get_analysis("auditor", first["analysis_id"])
        self.assertEqual(stored_first["algorithm_version"], ALGORITHM_VERSION)
        self.assertEqual(stored_first["input_sha256"], first["input_sha256"])
        self.assertEqual(stored_first["rule_set_version"], 1)
        self.assertEqual(stored_first["result"]["rates"]["pass"], "1.000000")
        self.assertEqual(second["result"]["rates"]["pass"], "0.000000")
        replay = self.service.run_analysis("analyst", "batch-1", rule_set_version=2)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["analysis_id"], second["analysis_id"])
        count = self.connection.execute("SELECT count(*) FROM analyses").fetchone()[0]
        self.assertEqual(count, 2)

    def test_analysis_cannot_use_earlier_rule_version(self) -> None:
        stricter = dict(RULE_SET_V1, version=2, indicator=dict(RULE_SET_V1["indicator"], threshold="0.95"))
        self.service.publish_rule_set("analyst", stricter)
        self.service.create_batch("op", "batch-2", "gdti-quarterly", 2, ["economy-a"])
        rows = [
            report_row("office-a", "economy-a", period, 1, value)
            for period, value in (("2026-Q1", "0.90"), ("2026-Q2", "0.91"), ("2026-Q3", "0.92"))
        ]
        self.service.import_observations("op", "batch-2", "key-1", rows)
        self.service.freeze_batch("analyst", "batch-2", 1)
        with self.assertRaises(ValidationFailed):
            self.service.run_analysis("analyst", "batch-2", rule_set_version=1)
        with self.assertRaises(NotFound):
            self.service.run_analysis("analyst", "batch-2", rule_set_version=99)

    def test_decision_only_on_latest_analysis_and_immutable(self) -> None:
        first = self._analyze(self._rows_full_pass())
        self.service.publish_decision("approver", "batch-1", first["analysis_id"], "approved_for_aggregation", "达标")
        with self.assertRaises(Conflict):
            self.service.publish_decision("approver", "batch-1", first["analysis_id"], "rejected", "改判")
        stricter = dict(RULE_SET_V1, version=2, indicator=dict(RULE_SET_V1["indicator"], threshold="0.95"))
        self.service.publish_rule_set("analyst", stricter)
        second = self.service.run_analysis("analyst", "batch-1", rule_set_version=2)
        with self.assertRaises(InvalidState):
            self.service.publish_decision("approver", "batch-1", first["analysis_id"], "approved_for_aggregation", "重复")
        self.service.publish_decision("approver", "batch-1", second["analysis_id"], "rejected", "新规则下不达标")
        report = self.service.report("auditor", "batch-1")
        self.assertEqual(len(report["decisions"]), 2)
        self.assertEqual(report["decisions"][0]["decision"], "approved_for_aggregation")
        self.assertEqual(report["current_decision"]["decision"], "rejected")
        self.assertEqual(report["current_decision"]["analysis_id"], second["analysis_id"])

    def test_published_decision_is_not_changed_by_reanalysis(self) -> None:
        first = self._analyze(self._rows_full_pass())
        self.service.publish_decision("approver", "batch-1", first["analysis_id"], "approved_for_aggregation", "达标")
        stricter = dict(RULE_SET_V1, version=2, indicator=dict(RULE_SET_V1["indicator"], threshold="0.95"))
        self.service.publish_rule_set("analyst", stricter)
        self.service.run_analysis("analyst", "batch-1", rule_set_version=2)
        report = self.service.report("auditor", "batch-1")
        self.assertEqual(report["current_decision"]["analysis_id"], first["analysis_id"])
        self.assertEqual(report["current_decision"]["decision"], "approved_for_aggregation")
        self.assertEqual(report["latest_analysis"]["rule_set_version"], 2)

    def test_import_after_freeze_and_analysis_before_freeze_are_rejected(self) -> None:
        with self.assertRaises(InvalidState):
            self.service.run_analysis("analyst", "batch-1")
        self.service.import_observations("op", "batch-1", "key-1", self._rows_full_pass())
        self.service.freeze_batch("analyst", "batch-1", 1)
        with self.assertRaises(InvalidState):
            self.service.import_observations("op", "batch-1", "key-2", self._rows_full_pass())
        with self.assertRaises(InvalidState):
            self.service.freeze_batch("analyst", "batch-1", 2)

    def test_invalid_observation_inputs_are_business_errors(self) -> None:
        cases = [
            [report_row("office-a", "economy-a", "2026-Q4", 1, "0.9")],
            [report_row("office-a", "economy-c", "2026-Q1", 1, "0.9")],
            [report_row("ghost", "economy-a", "2026-Q1", 1, "0.9")],
            [report_row("office-a", "economy-a", "2026-Q1", 1, "abc")],
            [report_row("office-a", "economy-a", "2026-Q1", 1, None)],
            [dict(retraction_row("office-a", "economy-a", "2026-Q1", 1), value="0.9")],
        ]
        for index, rows in enumerate(cases):
            with self.assertRaises(ValidationFailed, msg=str(rows)):
                self.service.import_observations("op", "batch-1", f"bad-{index}", rows)
        with self.assertRaises(NotFound):
            self.service.import_observations("op", "missing-batch", "key-1", self._rows_full_pass())
        with self.assertRaises(ValidationFailed):
            self.service.import_observations("op", "batch-1", "key-empty", [])

    def test_import_idempotency_replay_and_conflict(self) -> None:
        rows = self._rows_full_pass()
        first = self.service.import_observations("op", "batch-1", "key-1", rows)
        second = self.service.import_observations("op", "batch-1", "key-1", rows)
        self.assertEqual(first, second)
        changed = [dict(item) for item in rows]
        changed[0] = dict(changed[0], value="0.99")
        with self.assertRaises(Conflict):
            self.service.import_observations("op", "batch-1", "key-1", changed)
        count = self.connection.execute("SELECT count(*) FROM observation_records").fetchone()[0]
        self.assertEqual(count, 6)

    def test_rule_set_validation(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.publish_rule_set("analyst", dict(RULE_SET_V1, version=0))
        with self.assertRaises(ValidationFailed):
            self.service.publish_rule_set("analyst", dict(
                RULE_SET_V1, version=2,
                freeze_rules={"min_periods": 4, "max_failed_periods": 0, "source_priority": ["office-a"]},
            ))
        with self.assertRaises(ValidationFailed):
            self.service.publish_rule_set("analyst", dict(
                RULE_SET_V1, version=2,
                freeze_rules={"min_periods": 2, "max_failed_periods": 2, "source_priority": ["office-a"]},
            ))
        with self.assertRaises(Conflict):
            self.service.publish_rule_set("analyst", RULE_SET_V1)

    def test_role_separation(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.publish_rule_set("op", RULE_SET_V1)
        with self.assertRaises(Forbidden):
            self.service.freeze_batch("op", "batch-1", 1)
        with self.assertRaises(Forbidden):
            self.service.import_observations("analyst", "batch-1", "key-1", self._rows_full_pass())
        with self.assertRaises(Forbidden):
            self.service.report("op", "batch-1")
        with self.assertRaises(Forbidden):
            self.service.audit_trail("op", "batch-1")

    def test_report_traces_rate_to_sample_conclusions_and_audit(self) -> None:
        analysis = self._analyze(self._rows_full_pass())
        self.service.publish_decision("approver", "batch-1", analysis["analysis_id"], "approved_for_aggregation", "达标")
        report = self.service.report("auditor", "batch-1")
        latest = report["latest_analysis"]
        self.assertEqual(latest["input_sha256"], analysis["input_sha256"])
        self.assertEqual(latest["algorithm_version"], ALGORITHM_VERSION)
        self.assertEqual(latest["result"]["rates"]["pass"], "1.000000")
        self.assertEqual({item["sample_id"] for item in latest["result"]["samples"]}, {"economy-a", "economy-b"})
        conclusion = self.service.get_sample_conclusion("auditor", analysis["analysis_id"], "economy-a")
        self.assertEqual(conclusion["conclusion"], "pass")
        self.assertEqual(len(conclusion["periods"]), 3)
        with self.assertRaises(NotFound):
            self.service.get_sample_conclusion("auditor", analysis["analysis_id"], "economy-x")
        trail = self.service.audit_trail("auditor", "batch-1")
        event_types = [event["event_type"] for event in trail]
        self.assertEqual(
            event_types,
            ["batch.created", "observations.imported", "batch.frozen", "analysis.completed", "decision.published"],
        )
        self.assertEqual(trail[3]["payload"]["analysis_id"], analysis["analysis_id"])
        self.assertEqual(trail[3]["payload"]["input_sha256"], analysis["input_sha256"])
        self.assertEqual(trail[4]["payload"]["analysis_id"], analysis["analysis_id"])


class MetricQualityApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(MetricQualityService(self.connection))

    def tearDown(self) -> None:
        self.connection.close()

    def _post(self, path: str, payload: dict, actor: str | None = None, headers: dict | None = None):
        all_headers = dict(headers or {})
        if actor is not None:
            all_headers["X-Actor-Id"] = actor
        return self.app.handle("POST", path, all_headers, json.dumps(payload).encode())

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_error_shape(self) -> None:
        response = self.app.handle("POST", "/users", body=b"not-json")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")
        response = self.app.handle("GET", "/nope")
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body["error"]["code"], "route_not_found")

    def test_full_http_flow(self) -> None:
        for user_id, role in (("op", "operator"), ("analyst", "analyst"), ("approver", "approver"), ("auditor", "auditor")):
            self.assertEqual(self._post("/users", {"user_id": user_id, "display_name": user_id, "role": role}).status, 201)
        self.assertEqual(
            self._post("/sources", {"source_id": "office-a", "display_name": "甲统计办", "organization": "甲统计局"}, actor="op").status,
            201,
        )
        self.assertEqual(self._post("/rule_sets", RULE_SET_V1, actor="analyst").status, 201)
        created = self._post(
            "/batches",
            {"batch_id": "b1", "rule_set_id": "gdti-quarterly", "rule_set_version": 1, "sample_ids": ["economy-a"]},
            actor="op",
        )
        self.assertEqual(created.status, 201)
        self.assertEqual(created.body["sample_ids"], ["economy-a"])
        rows = [
            report_row("office-a", "economy-a", period, 1, value)
            for period, value in (("2026-Q1", "0.9"), ("2026-Q2", "0.91"), ("2026-Q3", "0.92"))
        ]
        imported = self._post("/batches/b1/observations", {"observations": rows}, actor="op", headers={"Idempotency-Key": "k1"})
        self.assertEqual(imported.status, 200)
        self.assertEqual(imported.body["inserted"], 3)
        self.assertEqual(self._post("/batches/b1/freeze", {"expected_revision": 1}, actor="analyst").status, 200)
        analysis = self._post("/batches/b1/analysis", {}, actor="analyst")
        self.assertEqual(analysis.status, 200)
        self.assertEqual(analysis.body["result"]["rates"]["pass"], "1.000000")
        analysis_id = analysis.body["analysis_id"]
        fetched = self.app.handle("GET", f"/analyses/{analysis_id}", {"X-Actor-Id": "auditor"})
        self.assertEqual(fetched.status, 200)
        traced = self.app.handle("GET", f"/analyses/{analysis_id}/samples/economy-a", {"X-Actor-Id": "auditor"})
        self.assertEqual(traced.status, 200)
        self.assertEqual(traced.body["conclusion"], "pass")
        decided = self._post(
            "/decisions",
            {"batch_id": "b1", "analysis_id": analysis_id, "decision": "approved_for_aggregation", "reason": "达标"},
            actor="approver",
        )
        self.assertEqual(decided.status, 201)
        report = self.app.handle("GET", "/batches/b1/report", {"X-Actor-Id": "auditor"})
        self.assertEqual(report.status, 200)
        self.assertEqual(report.body["current_decision"]["decision"], "approved_for_aggregation")
        audit = self.app.handle("GET", "/batches/b1/audit", {"X-Actor-Id": "auditor"})
        self.assertEqual(audit.status, 200)
        self.assertEqual(audit.body["events"][-1]["event_type"], "decision.published")
        forbidden = self.app.handle("GET", "/batches/b1/report", {"X-Actor-Id": "op"})
        self.assertEqual(forbidden.status, 403)
        self.assertEqual(forbidden.body["error"]["code"], "forbidden")


class AcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = acceptance_run()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["algorithm_version"], ALGORITHM_VERSION)
        self.assertEqual(result["sample_count"], 2)
        self.assertEqual(result["conclusions"], {"pass": 2, "reject": 0, "insufficient_evidence": 0})
        self.assertEqual(result["rates"]["pass"], "1.000000")
        self.assertEqual(result["decision"], "approved_for_aggregation")
        self.assertEqual(len(result["input_sha256"]), 64)
        self.assertEqual(result["schema"]["missing_tables"], [])


if __name__ == "__main__":
    unittest.main()
