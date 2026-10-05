from __future__ import annotations

import unittest
from decimal import Decimal

from metric_quality.quality import (
    ALGORITHM_VERSION,
    FrozenPolicy,
    ObservationRecord,
    QualityInputError,
    evaluate,
    freeze_series,
)


def policy(**overrides) -> FrozenPolicy:
    raw = {
        "policy_id": "p",
        "version": 1,
        "indicator_id": "idx",
        "reporter_ids": ["A", "B"],
        "periods": ["p1", "p2"],
        "source_priority": ["official", "gateway"],
        "operator": "gte",
        "threshold": "0.8",
        "conflict_tolerance": "0.01",
        "require_all_periods": True,
    }
    raw.update(overrides)
    return FrozenPolicy.from_dict(raw)


def rec(sample, period, kind="report", *, at="2026-01-01T00:00:00+00:00", value="0.9",
        source="official", reporter="A", rid=None):
    return ObservationRecord(
        reporter_id=reporter, sample_id=sample, indicator_id="idx", period=period,
        kind=kind, recorded_at=at,
        value=None if value is None else Decimal(value),
        source_id=source if kind in ("report", "resubmission") else None,
        record_id=rid or f"{sample}-{period}-{kind}-{at}",
    )


class FreezeRulesTests(unittest.TestCase):
    def test_pass_when_all_periods_meet_threshold(self) -> None:
        result = freeze_series(policy(), "s1", "A", [
            rec("s1", "p1", value="0.81"), rec("s1", "p2", value="0.95", at="2026-02-01T00:00:00+00:00"),
        ])
        self.assertEqual(result["conclusion"], "pass")

    def test_one_failed_period_rejects_sample(self) -> None:
        result = freeze_series(policy(), "s1", "A", [
            rec("s1", "p1", value="0.9"), rec("s1", "p2", value="0.7", at="2026-02-01T00:00:00+00:00"),
        ])
        self.assertEqual(result["conclusion"], "reject")
        self.assertTrue(any(reason.startswith("threshold_failed:p2") for reason in result["reasons"]))

    def test_absent_period_is_missing_and_insufficient(self) -> None:
        result = freeze_series(policy(), "s1", "A", [rec("s1", "p1", value="0.9")])
        self.assertEqual(result["conclusion"], "insufficient")
        self.assertEqual(result["periods"][1]["status"], "missing")

    def test_explicit_missing_marker_late_means_insufficient(self) -> None:
        result = freeze_series(policy(), "s1", "A", [
            rec("s1", "p1", value="0.9"),
            rec("s1", "p2", value="0.95", at="2026-02-01T00:00:00+00:00"),
            rec("s1", "p2", kind="missing", at="2026-03-01T00:00:00+00:00", value=None, source=None),
        ])
        self.assertEqual(result["conclusion"], "insufficient")
        self.assertEqual(result["periods"][1]["status"], "missing")

    def test_withdrawal_marker_voids_period(self) -> None:
        result = freeze_series(policy(), "s1", "A", [
            rec("s1", "p1", value="0.9"),
            rec("s1", "p2", value="0.95", at="2026-02-01T00:00:00+00:00"),
            rec("s1", "p2", kind="withdrawal", at="2026-03-01T00:00:00+00:00", value=None, source=None),
        ])
        self.assertEqual(result["conclusion"], "insufficient")
        self.assertEqual(result["periods"][1]["status"], "withdrawn")

    def test_new_resubmission_after_withdrawal_revives_period(self) -> None:
        result = freeze_series(policy(), "s1", "A", [
            rec("s1", "p1", value="0.9"),
            rec("s1", "p2", value="0.5", at="2026-02-01T00:00:00+00:00"),
            rec("s1", "p2", kind="withdrawal", at="2026-03-01T00:00:00+00:00", value=None, source=None),
            rec("s1", "p2", kind="resubmission", value="0.9", at="2026-04-01T00:00:00+00:00"),
        ])
        self.assertEqual(result["conclusion"], "pass")
        self.assertEqual(result["periods"][1]["status"], "reported")
        self.assertEqual(result["periods"][1]["record_kind"], "resubmission")

    def test_resubmission_supersedes_earlier_report_same_source(self) -> None:
        result = freeze_series(policy(), "s1", "A", [
            rec("s1", "p1", value="0.5", at="2026-01-01T00:00:00+00:00", rid="old"),
            rec("s1", "p2", value="0.9", at="2026-02-01T00:00:00+00:00"),
            rec("s1", "p1", kind="resubmission", value="0.95", at="2026-01-05T00:00:00+00:00", rid="new"),
        ])
        self.assertEqual(result["conclusion"], "pass")
        self.assertEqual(result["periods"][0]["value"], "0.95")
        self.assertIn("old", result["periods"][0]["superseded_record_ids"])

    def test_conflicting_sources_beyond_tolerance_is_insufficient(self) -> None:
        result = freeze_series(policy(), "s1", "A", [
            rec("s1", "p1", value="0.95", source="gateway", at="2026-01-01T00:00:00+00:00"),
            rec("s1", "p1", value="0.70", source="official", at="2026-01-02T00:00:00+00:00"),
            rec("s1", "p2", value="0.9", at="2026-02-01T00:00:00+00:00"),
        ])
        self.assertEqual(result["conclusion"], "insufficient")
        self.assertEqual(result["periods"][0]["status"], "conflict")
        self.assertEqual(len(result["periods"][0]["conflicting_sources"]), 2)

    def test_values_within_tolerance_pick_highest_priority_source(self) -> None:
        result = freeze_series(policy(), "s1", "A", [
            rec("s1", "p1", value="0.815", source="gateway", at="2026-01-01T00:00:00+00:00"),
            rec("s1", "p1", value="0.810", source="official", at="2026-01-02T00:00:00+00:00"),
            rec("s1", "p2", value="0.9", at="2026-02-01T00:00:00+00:00"),
        ])
        self.assertEqual(result["conclusion"], "pass")
        self.assertEqual(result["periods"][0]["source_id"], "official")
        self.assertEqual(result["periods"][0]["value"], "0.810")

    def test_equal_timestamps_kind_rank_makes_resubmission_win(self) -> None:
        stamp = "2026-01-01T00:00:00+00:00"
        result = freeze_series(policy(), "s1", "A", [
            rec("s1", "p1", value="0.5", at=stamp, rid="first"),
            rec("s1", "p1", kind="resubmission", value="0.9", at=stamp, rid="second"),
            rec("s1", "p2", value="0.9", at="2026-02-01T00:00:00+00:00"),
        ])
        self.assertEqual(result["periods"][0]["value"], "0.9")

    def test_undeclared_period_rejected(self) -> None:
        with self.assertRaises(QualityInputError):
            freeze_series(policy(), "s1", "A", [
                rec("s1", "p1"), rec("s1", "p2"),
                rec("s1", "p3", at="2026-03-01T00:00:00+00:00"),
            ])


class EvaluateTests(unittest.TestCase):
    def _two_economy_records(self):
        return [
            rec("s1", "p1", value="0.9", reporter="A"),
            rec("s1", "p2", value="0.9", at="2026-02-01T00:00:00+00:00", reporter="A"),
            rec("s2", "p1", value="0.9", reporter="A"),  # missing p2
            rec("s3", "p1", value="0.9", reporter="B", source="gateway"),
            rec("s3", "p2", value="0.6", at="2026-02-01T00:00:00+00:00", reporter="B", source="gateway"),
        ]

    def test_rates_are_over_samples_and_sum_to_one(self) -> None:
        result = evaluate(policy(), self._two_economy_records())
        self.assertEqual(result["sample_count"], 3)
        self.assertEqual(result["counts"], {"pass": 1, "reject": 1, "insufficient": 1})
        rates = result["rates"]
        total = sum(Decimal(rates[key]) for key in ("pass", "reject", "insufficient"))
        self.assertEqual(total, Decimal(1))
        self.assertLessEqual(Decimal(rates["pass"]), Decimal(1))

    def test_passing_observation_points_cannot_exceed_sample_denominator(self) -> None:
        # 旧缺陷：一个样本三期达标被算作三个合格点，通过率超过 100%。
        records = [
            rec("s1", "p1", value="0.95"),
            rec("s1", "p2", value="0.95", at="2026-02-01T00:00:00+00:00"),
            rec("s2", "p1", value="0.95", reporter="B", source="gateway"),
            rec("s2", "p2", value="0.5", at="2026-02-01T00:00:00+00:00", reporter="B", source="gateway"),
        ]
        result = evaluate(policy(), records)
        self.assertEqual(result["record_count"], 4)
        self.assertEqual(result["sample_count"], 2)
        self.assertEqual(result["rates"]["pass"], "0.500000")

    def test_by_reporter_breakdown(self) -> None:
        result = evaluate(policy(), self._two_economy_records())
        by = {item["reporter_id"]: item for item in result["by_reporter"]}
        self.assertEqual(by["A"]["sample_count"], 2)
        self.assertEqual(by["A"]["counts"]["pass"], 1)
        self.assertEqual(by["B"]["counts"]["reject"], 1)

    def test_deterministic(self) -> None:
        records = self._two_economy_records()
        first = evaluate(policy(), records)
        second = evaluate(policy(), list(reversed(records)))
        self.assertEqual(first, second)
        self.assertEqual(first["algorithm_version"], ALGORITHM_VERSION)

    def test_sample_cannot_belong_to_two_reporters(self) -> None:
        with self.assertRaises(QualityInputError):
            evaluate(policy(), [
                rec("s1", "p1", reporter="A"),
                rec("s1", "p2", reporter="B", source="gateway", at="2026-02-01T00:00:00+00:00"),
            ])

    def test_unregistered_reporter_rejected(self) -> None:
        with self.assertRaises(QualityInputError):
            evaluate(policy(), [rec("s1", "p1", reporter="C")])

    def test_unregistered_source_rejected(self) -> None:
        with self.assertRaises(QualityInputError):
            evaluate(policy(), [rec("s1", "p1", source="unknown")])

    def test_indicator_mismatch_rejected(self) -> None:
        bad = ObservationRecord(
            reporter_id="A", sample_id="s1", indicator_id="other", period="p1",
            kind="report", recorded_at="2026-01-01T00:00:00+00:00", value=Decimal("0.9"), source_id="official",
        )
        with self.assertRaises(QualityInputError):
            evaluate(policy(), [bad])

    def test_empty_input_rejected(self) -> None:
        with self.assertRaises(QualityInputError):
            evaluate(policy(), [])

    def test_partial_coverage_mode_still_conflicts_but_counts_reported_periods(self) -> None:
        partial = policy(require_all_periods=False)
        result = evaluate(partial, [
            rec("s1", "p1", value="0.9"),  # no p2: only reported periods count
        ])
        self.assertEqual(result["samples"][0]["conclusion"], "pass")


class PolicyContractTests(unittest.TestCase):
    def test_supersedes_must_be_older(self) -> None:
        with self.assertRaises(QualityInputError):
            policy(version=2, supersedes_version=2)

    def test_duplicate_periods_rejected(self) -> None:
        with self.assertRaises(QualityInputError):
            policy(periods=["p1", "p1"])

    def test_marker_record_cannot_carry_value(self) -> None:
        with self.assertRaises(QualityInputError):
            ObservationRecord.from_dict({
                "reporter_id": "A", "sample_id": "s", "indicator_id": "idx",
                "period": "p1", "kind": "missing", "recorded_at": "2026-01-01T00:00:00+00:00",
                "value": "0.9",
            })

    def test_non_finite_number_rejected(self) -> None:
        with self.assertRaises(QualityInputError):
            policy(threshold="nan")


if __name__ == "__main__":
    unittest.main()
