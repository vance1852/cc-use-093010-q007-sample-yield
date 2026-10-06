"""统计资料质量流程的离线验收入口。"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from .service import MetricQualityService
from .storage import connect, inspect_schema


RULE_SET = {
    "rule_set_id": "gdti-quarterly",
    "version": 1,
    "title": "全球数字贸易指数季度质量规则",
    "indicator": {
        "key": "digital_trade_index",
        "label": "数字贸易指数",
        "direction": "higher",
        "threshold": "0.8",
    },
    "expected_periods": ["2026-Q1", "2026-Q2", "2026-Q3"],
    "freeze_rules": {
        "min_periods": 3,
        "max_failed_periods": 0,
        "source_priority": ["stats-office-a", "stats-office-b", "secretariat-model"],
    },
}


def _observations() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for period, value in (("2026-Q1", "0.84"), ("2026-Q2", "0.91"), ("2026-Q3", "0.87")):
        rows.append({
            "source_id": "stats-office-a",
            "sample_id": "economy-a",
            "period": period,
            "revision": 1,
            "kind": "report",
            "value": value,
        })
    rows.extend([
        # 经济体乙 2026-Q1：两个来源数值冲突，按来源优先级取统计办公室
        {"source_id": "stats-office-b", "sample_id": "economy-b", "period": "2026-Q1", "revision": 1, "kind": "report", "value": "0.86"},
        {"source_id": "secretariat-model", "sample_id": "economy-b", "period": "2026-Q1", "revision": 1, "kind": "report", "value": "0.68"},
        # 经济体乙 2026-Q2：复报修正，第二版取代第一版
        {"source_id": "stats-office-b", "sample_id": "economy-b", "period": "2026-Q2", "revision": 1, "kind": "report", "value": "0.62"},
        {"source_id": "stats-office-b", "sample_id": "economy-b", "period": "2026-Q2", "revision": 2, "kind": "report", "value": "0.93"},
        # 经济体乙 2026-Q3：统计办公室撤销原报，改由秘书处估算值生效
        {"source_id": "stats-office-b", "sample_id": "economy-b", "period": "2026-Q3", "revision": 1, "kind": "report", "value": "0.71"},
        {"source_id": "stats-office-b", "sample_id": "economy-b", "period": "2026-Q3", "revision": 2, "kind": "retraction", "value": None},
        {"source_id": "secretariat-model", "sample_id": "economy-b", "period": "2026-Q3", "revision": 1, "kind": "report", "value": "0.88"},
    ])
    return rows


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="metric-quality-") as temporary:
        connection = connect(Path(temporary) / "metric-quality.sqlite3")
        try:
            service = MetricQualityService(connection)
            service.create_user("operator-1", "数据报送操作员", "operator")
            service.create_user("analyst-1", "统计分析员", "analyst")
            service.create_user("approver-1", "质量审批人", "approver")
            service.create_user("auditor-1", "审计人员", "auditor")
            service.register_source("operator-1", "stats-office-a", "经济体甲统计办公室", "甲经济体统计局")
            service.register_source("operator-1", "stats-office-b", "经济体乙统计办公室", "乙经济体统计局")
            service.register_source("operator-1", "secretariat-model", "秘书处估算模型", "全球数字贸易秘书处")
            service.publish_rule_set("analyst-1", RULE_SET)
            service.create_batch(
                "operator-1", "gdti-2026-q1-q3", RULE_SET["rule_set_id"], RULE_SET["version"],
                ["economy-a", "economy-b"],
            )
            service.import_observations("operator-1", "gdti-2026-q1-q3", "acceptance-import-1", _observations())
            service.freeze_batch("analyst-1", "gdti-2026-q1-q3", 1)
            analysis = service.run_analysis("analyst-1", "gdti-2026-q1-q3")
            service.publish_decision(
                "approver-1", "gdti-2026-q1-q3", analysis["analysis_id"],
                "approved_for_aggregation", "两个经济体样本均达标，准予进入全球汇总",
            )
            report = service.report("auditor-1", "gdti-2026-q1-q3")
            schema = inspect_schema(connection)
        finally:
            connection.close()
    if schema["missing_tables"]:
        raise RuntimeError("SQLite 基础结构检查失败")
    result = analysis["result"]
    return {
        "status": "ok",
        "batch_id": "gdti-2026-q1-q3",
        "analysis_id": analysis["analysis_id"],
        "algorithm_version": analysis["algorithm_version"],
        "input_sha256": analysis["input_sha256"],
        "sample_count": result["sample_count"],
        "conclusions": result["conclusions"],
        "rates": result["rates"],
        "traced_samples": [sample["sample_id"] for sample in result["samples"]],
        "decision": report["current_decision"]["decision"],
        "event_count": len(report["events"]),
        "schema": schema,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行统计资料质量流程的离线自检")
    parser.parse_args(argv)
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
