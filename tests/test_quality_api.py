from __future__ import annotations

import json
import unittest

from metric_quality.api import JsonApplication
from metric_quality.service import MetricQualityService

POLICY = {
    "policy_id": "q",
    "version": 1,
    "indicator_id": "idx",
    "reporter_ids": ["A"],
    "periods": ["2025", "2026"],
    "source_priority": ["official"],
    "operator": "gte",
    "threshold": "0.8",
}


def record(rid, period="2025", value="0.9", kind="report"):
    item = {
        "record_id": rid, "reporter_id": "A", "sample_id": "s1",
        "indicator_id": "idx", "period": period, "kind": kind,
        "recorded_at": "2026-01-01T00:00:00+00:00",
    }
    if value is not None:
        item["value"] = value
    if kind in ("report", "resubmission"):
        item["source_id"] = "official"
    return item


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MetricQualityService(":memory:")
        self.service.bootstrap_admin()
        admin = self.service.auth.login("admin", "metric-admin")
        self.service.auth.create_user("rep", "rep-password", "reporter")
        self.service.auth.create_user("ana", "ana-password", "analyst")
        self.service.auth.create_user("app", "app-password", "approver")
        self.service.auth.create_user("aud", "aud-password", "auditor")
        self.reporter = self.service.auth.login("rep", "rep-password")
        self.analyst = self.service.auth.login("ana", "ana-password")
        self.approver = self.service.auth.login("app", "app-password")
        self.auditor = self.service.auth.login("aud", "aud-password")
        self.app = JsonApplication(self.service)
        status, body = self.app.handle_response("POST", "/policies", self._headers(admin),
                                                json.dumps(POLICY).encode())
        self.assertEqual(status, 201)

    def _headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _call(self, method, path, token, payload=None):
        body = b"" if payload is None else json.dumps(payload).encode()
        return self.app.handle_response(method, path, self._headers(token), body)

    def test_health(self) -> None:
        status, body = self.app.handle_response("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_login_and_route_not_found(self) -> None:
        status, body = self.app.handle_response("POST", "/login", body=json.dumps({
            "user_id": "admin", "password": "metric-admin",
        }).encode())
        self.assertEqual(status, 200)
        self.assertIn("token", body)
        status, body = self.app.handle_response("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "route_not_found")

    def test_invalid_json_returns_understandable_error(self) -> None:
        status, body = self.app.handle_response(
            "POST", "/policies/q/versions/1/records", self._headers(self.reporter), b"not-json"
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_request")
        self.assertIn("JSON", body["error"]["message"])

    def test_bad_record_returns_validation_code(self) -> None:
        bad = record("r1")
        del bad["sample_id"]
        status, body = self._call("POST", "/policies/q/versions/1/records", self.reporter, {"records": [bad]})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "validation_failed")
        self.assertTrue(body["error"]["message"])

    def test_forbidden_role_returns_403(self) -> None:
        status, body = self._call("POST", "/policies/q/versions/1/analysis", self.reporter)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")

    def test_missing_token_is_forbidden(self) -> None:
        status, body = self.app.handle_response("GET", "/policies/q")
        self.assertEqual(status, 403)

    def test_full_route_flow_and_drilldown(self) -> None:
        status, _ = self._call("POST", "/policies/q/versions/1/records", self.reporter, {"records": [
            record("r1", "2025"), record("r2", "2026", value="0.95"),
        ]})
        self.assertEqual(status, 200)
        status, analysis = self._call("POST", "/policies/q/versions/1/analysis", self.analyst)
        self.assertEqual(status, 200)
        self.assertEqual(analysis["result"]["rates"]["pass"], "1.000000")
        analysis_id = analysis["analysis_id"]
        # 下钻：分析视图含样本结论与原始记录
        status, fetched = self._call("GET", f"/analyses/{analysis_id}", self.auditor)
        self.assertEqual(status, 200)
        self.assertEqual(fetched["result"]["samples"][0]["conclusion"], "pass")
        self.assertEqual({r["record_id"] for r in fetched["records"]}, {"r1", "r2"})
        # 决定与撤销
        status, decided = self._call("POST", f"/analyses/{analysis_id}/decisions", self.approver,
                                     {"decision": "release", "reason": "通过"})
        self.assertEqual(status, 201)
        status, _ = self._call("POST", f"/analyses/{analysis_id}/revoke", self.approver,
                               {"reason": "需重评"})
        self.assertEqual(status, 200)
        # 审计接口可按实体过滤
        status, events = self._call("GET", "/audit?entity_type=decision", self.auditor)
        self.assertEqual(status, 200)
        self.assertEqual({e["event_type"] for e in events}, {"decision.recorded", "decision.revoked"})

    def test_legacy_endpoint_preserves_old_version(self) -> None:
        status, body = self._call("POST", "/legacy-analyses", self.analyst, {
            "lot_id": "LOT-1",
            "input_summary": {"sample_count": 2, "measurement_count": 5},
            "result": {"yield": {"yield": 1.4}},
        })
        self.assertEqual(status, 201)
        self.assertEqual(body["algorithm_version"], "metric-quality-observation-yield/1")
        status, listed = self._call("GET", "/lots/LOT-1/legacy-analyses", self.auditor)
        self.assertEqual(status, 200)
        self.assertEqual(len(listed), 1)


if __name__ == "__main__":
    unittest.main()
