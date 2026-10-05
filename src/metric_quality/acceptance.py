"""离线冒烟验收：两个经济体多期报送同一指标的质量冻结流程。

场景覆盖：复报取代、缺期、撤销、跨来源冲突、样本身份跨主体冲突、
旧版本分析原样保留，以及从总体通过率下钻到样本结论与原始记录。
"""

from __future__ import annotations

import argparse
import json

from .service import MetricQualityService

POLICY_V1 = {
    "policy_id": "dtsi-quality",
    "version": 1,
    "indicator_id": "digital-trade-services-index",
    "reporter_ids": ["economy-A", "economy-B"],
    "periods": ["2025-Q4", "2026-Q1", "2026-Q2"],
    "source_priority": ["national-portal", "secretariat-gateway"],
    "operator": "gte",
    "threshold": "0.80",
    "conflict_tolerance": "0.02",
    "require_all_periods": True,
}

POLICY_V2 = {
    **POLICY_V1,
    "version": 2,
    "threshold": "0.75",
    "supersedes_version": 1,
}


def run() -> dict:
    service = MetricQualityService()
    service.bootstrap_admin()
    admin = service.auth.login("admin", "metric-admin")
    for user_id, role in (("reporter", "reporter"), ("analyst", "analyst"), ("approver", "approver"), ("auditor", "auditor")):
        service.auth.create_user(user_id, f"{user_id}-pass-1", role)
    reporter = service.auth.login("reporter", "reporter-pass-1")
    analyst = service.auth.login("analyst", "analyst-pass-1")
    approver = service.auth.login("approver", "approver-pass-1")
    auditor = service.auth.login("auditor", "auditor-pass-1")

    service.publish_policy(admin, POLICY_V1)

    def rec(record_id, reporter_id, sample_id, period, kind, recorded_at, value=None, source_id=None):
        row = {
            "record_id": record_id, "reporter_id": reporter_id, "sample_id": sample_id,
            "indicator_id": POLICY_V1["indicator_id"], "period": period, "kind": kind,
            "recorded_at": recorded_at,
        }
        if value is not None:
            row["value"] = value
        if source_id is not None:
            row["source_id"] = source_id
        return row

    records = [
        # A-1：三期全部达标，其中一期先报后复报（复报取代初报）。
        rec("r-a1-q4", "economy-A", "sample-A-1", "2025-Q4", "report", "2026-01-10T08:00:00+00:00", "0.86", "national-portal"),
        rec("r-a1-q1", "economy-A", "sample-A-1", "2026-Q1", "report", "2026-04-11T08:00:00+00:00", "0.79", "national-portal"),
        rec("r-a1-q1-fix", "economy-A", "sample-A-1", "2026-Q1", "resubmission", "2026-04-20T08:00:00+00:00", "0.91", "national-portal"),
        rec("r-a1-q2", "economy-A", "sample-A-1", "2026-Q2", "report", "2026-07-12T08:00:00+00:00", "0.88", "national-portal"),
        # A-2：缺一期 → 证据不足。
        rec("r-a2-q4", "economy-A", "sample-A-2", "2025-Q4", "report", "2026-01-11T08:00:00+00:00", "0.90", "national-portal"),
        rec("r-a2-q2", "economy-A", "sample-A-2", "2026-Q2", "report", "2026-07-13T08:00:00+00:00", "0.92", "national-portal"),
        # B-1：Q4 报送后被撤销 → 证据不足。
        rec("r-b1-q4", "economy-B", "sample-B-1", "2025-Q4", "report", "2026-01-12T08:00:00+00:00", "0.83", "secretariat-gateway"),
        rec("r-b1-q1", "economy-B", "sample-B-1", "2026-Q1", "report", "2026-04-12T08:00:00+00:00", "0.84", "secretariat-gateway"),
        rec("r-b1-q2", "economy-B", "sample-B-1", "2026-Q2", "report", "2026-07-14T08:00:00+00:00", "0.85", "secretariat-gateway"),
        # B-2：两个来源数值冲突超出容差 → 证据不足；另有一期不达标 → 拒绝。
        rec("r-b2-q4-gw", "economy-B", "sample-B-2", "2025-Q4", "report", "2026-01-13T08:00:00+00:00", "0.95", "secretariat-gateway"),
        rec("r-b2-q4-np", "economy-B", "sample-B-2", "2025-Q4", "report", "2026-01-14T08:00:00+00:00", "0.70", "national-portal"),
        rec("r-b2-q1", "economy-B", "sample-B-2", "2026-Q1", "report", "2026-04-13T08:00:00+00:00", "0.72", "national-portal"),
        rec("r-b2-q2", "economy-B", "sample-B-2", "2026-Q2", "report", "2026-07-15T08:00:00+00:00", "0.81", "national-portal"),
        # B-3：三期齐全且无冲突，但 2026-Q1 明确低于阈值 → 拒绝。
        rec("r-b3-q4", "economy-B", "sample-B-3", "2025-Q4", "report", "2026-01-15T08:00:00+00:00", "0.82", "national-portal"),
        rec("r-b3-q1", "economy-B", "sample-B-3", "2026-Q1", "report", "2026-04-15T08:00:00+00:00", "0.72", "national-portal"),
        rec("r-b3-q2", "economy-B", "sample-B-3", "2026-Q2", "report", "2026-07-16T08:00:00+00:00", "0.83", "national-portal"),
    ]
    service.import_records(reporter, "dtsi-quality", 1, records, idempotency_key="demo-import-1")
    service.revoke_record(reporter, "r-b1-q4", "源机构发现口径错误，撤销该期报送")

    v1 = service.run_analysis(analyst, "dtsi-quality", 1)
    # A-1 通过；B-3 明确低于阈值被拒绝；A-2 缺期、B-1 撤销、B-2 来源冲突均为证据不足。
    assert v1["result"]["counts"] == {"pass": 1, "reject": 1, "insufficient": 3}, v1["result"]["counts"]
    assert v1["result"]["rates"]["pass"] == "0.200000"
    rates = v1["result"]["rates"]
    assert float(rates["pass"]) + float(rates["reject"]) + float(rates["insufficient"]) == 1.0
    service.decide(approver, v1["analysis_id"], "hold", "证据不足样本占比过半，暂缓进入全球汇总")

    # 旧算法结果按其自身版本与输入摘要原样保留，不被新算法改写。
    legacy = service.register_legacy_analysis(
        analyst,
        "BATCH-DEMO",
        {"sample_count": 4, "measurement_count": 13, "threshold": 0.8},
        {"algorithm_version": "metric-quality-observation-yield/1", "yield": {"yield": 1.08, "reject_rate": 0.0}},
    )

    # 阈值放宽产生后继规则版本；旧决定撤销后才能在后继版本上重新决定。
    service.publish_policy(admin, POLICY_V2)
    v2_records = [
        # A-1 沿用原报送。
        *[row for row in records if row["record_id"].startswith("r-a1-")],
        # A-2 仍缺 2026-Q1，证据不足保持不变。
        *[row for row in records if row["record_id"].startswith("r-a2-")],
        # B-1 重新补报曾被撤销的 Q4。
        *[row for row in records if row["record_id"] in {"r-b1-q1", "r-b1-q2"}],
        rec("r-b1-q4-v2", "economy-B", "sample-B-1", "2025-Q4", "report", "2026-09-01T08:00:00+00:00", "0.83", "secretariat-gateway"),
        # B-2 冲突期只保留权威门户的更正值，不达标期复报更正。
        rec("r-b2-q4-v2", "economy-B", "sample-B-2", "2025-Q4", "report", "2026-09-02T08:00:00+00:00", "0.83", "national-portal"),
        rec("r-b2-q1-v2", "economy-B", "sample-B-2", "2026-Q1", "resubmission", "2026-09-02T08:00:00+00:00", "0.90", "national-portal"),
        rec("r-b2-q2", "economy-B", "sample-B-2", "2026-Q2", "report", "2026-07-15T08:00:00+00:00", "0.81", "national-portal"),
        # B-3 沿用原报送：0.72 即使在放宽后的 0.75 阈值下仍被拒绝。
        *[row for row in records if row["record_id"].startswith("r-b3-")],
    ]
    service.import_records(reporter, "dtsi-quality", 2, v2_records, idempotency_key="demo-import-2")
    v2 = service.run_analysis(analyst, "dtsi-quality", 2)
    assert v2["result"]["counts"] == {"pass": 3, "reject": 1, "insufficient": 1}, v2["result"]["counts"]
    service.revoke_decision(approver, v1["analysis_id"], "后继规则版本已发布，按新阈值重评")
    service.decide(approver, v2["analysis_id"], "release", "冲突已澄清，通过率满足全球汇总门槛")

    events = service.audit(auditor)
    # 下钻校验：总体通过率 -> 样本结论 -> 原始记录。
    conclusions = {item["sample_id"]: item["conclusion"] for item in v1["result"]["samples"]}
    assert conclusions == {
        "sample-A-1": "pass", "sample-A-2": "insufficient",
        "sample-B-1": "insufficient", "sample-B-2": "insufficient", "sample-B-3": "reject",
    }
    sample_b2 = next(item for item in v1["result"]["samples"] if item["sample_id"] == "sample-B-2")
    q4 = next(period for period in sample_b2["periods"] if period["period"] == "2025-Q4")
    assert q4["status"] == "conflict"
    return {
        "status": "ok",
        "v1_analysis_id": v1["analysis_id"],
        "v1_counts": v1["result"]["counts"],
        "v1_rates": v1["result"]["rates"],
        "v1_by_reporter": v1["result"]["by_reporter"],
        "v2_analysis_id": v2["analysis_id"],
        "v2_conclusions": {item["sample_id"]: item["conclusion"] for item in v2["result"]["samples"]},
        "legacy_algorithm": legacy["algorithm_version"],
        "audit_events": len(events),
        "drilldown_conflict_sources": q4["conflicting_sources"],
    }


def main() -> None:
    argparse.ArgumentParser().parse_args()
    print(json.dumps(run(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
