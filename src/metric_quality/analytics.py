"""按冻结规则归并样本观测序列并计算批次质量比例的确定性模型。

算法版本 2 取代按观测点计数的旧口径：每个样本（经济体 × 指标）先按冻结规则
把观测序列归并为唯一结论，再按样本总体计算通过、拒绝和证据不足比例，
因此任何比例都不会超过样本总体，也不会因观测点数量与样本数不一致而中止。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Iterable, Mapping, Sequence

from .contracts import RuleSet


ALGORITHM_VERSION = "metric-quality-analysis/2"

CONCLUSION_PASS = "pass"
CONCLUSION_REJECT = "reject"
CONCLUSION_INSUFFICIENT = "insufficient_evidence"
CONCLUSIONS = (CONCLUSION_PASS, CONCLUSION_REJECT, CONCLUSION_INSUFFICIENT)

_RATE_QUANTUM = Decimal("0.000001")


@dataclass(frozen=True, slots=True)
class SeriesRecord:
    """参与冻结归并的一条观测记录（含复报与撤销）。"""

    sample_id: str
    source_id: str
    period: str
    revision: int
    kind: str
    value: Decimal | None


def _rate(part: int, total: int) -> str:
    return format((Decimal(part) / Decimal(total)).quantize(_RATE_QUANTUM, rounding=ROUND_HALF_EVEN), "f")


def freeze_sample_series(records: Sequence[SeriesRecord], source_priority: Sequence[str]) -> dict[str, object]:
    """把同一样本的观测序列冻结为各期间唯一有效值。

    - 复报：同一报送主体在同一期间只保留最高版本，被取代的记录计入 superseded_records；
    - 撤销：某报送主体在某期间的最新记录为撤销时，该主体该期间缺席，撤销记录计入
      retracted_records；
    - 冲突：多个报送主体对同一期间给出不同数值时，按规则集来源优先级取唯一有效值，
      被舍弃的数值完整保留在 conflicts 中；数值一致的多源报送不构成冲突。
    """
    priority = {source_id: index for index, source_id in enumerate(source_priority)}
    latest: dict[tuple[str, str], SeriesRecord] = {}
    for record in records:
        key = (record.source_id, record.period)
        current = latest.get(key)
        if current is None or record.revision > current.revision:
            latest[key] = record
    superseded = len(records) - len(latest)
    retracted = 0
    candidates: dict[str, list[SeriesRecord]] = {}
    for (source_id, period), record in latest.items():
        if record.kind == "retraction":
            retracted += 1
            continue
        candidates.setdefault(period, []).append(record)
    periods: list[dict[str, object]] = []
    conflicts: list[dict[str, object]] = []
    for period in sorted(candidates):
        options = sorted(
            candidates[period],
            key=lambda item: (priority.get(item.source_id, len(priority)), item.source_id),
        )
        chosen = options[0]
        discarded = [item for item in options[1:] if item.value != chosen.value]
        if discarded:
            conflicts.append({
                "period": period,
                "chosen": {"source_id": chosen.source_id, "value": format(chosen.value, "f")},
                "discarded": [
                    {"source_id": item.source_id, "value": format(item.value, "f")}
                    for item in discarded
                ],
            })
        periods.append({
            "period": period,
            "value": chosen.value,
            "source_id": chosen.source_id,
            "revision": chosen.revision,
        })
    return {
        "periods": periods,
        "conflicts": conflicts,
        "superseded_records": superseded,
        "retracted_records": retracted,
    }


def _period_passes(value: Decimal, rule_set: RuleSet) -> bool:
    indicator = rule_set.indicator
    if indicator.direction == "higher":
        return value >= indicator.threshold
    return value <= indicator.threshold


def conclude_sample(sample_id: str, rule_set: RuleSet, frozen: Mapping[str, object]) -> dict[str, object]:
    """按冻结规则把单个样本的有效序列归并为唯一结论。

    判定顺序固定：有效期间数不足 min_periods → 证据不足；未达标期间数超过
    max_failed_periods → 拒绝；其余 → 通过。缺期期间始终列在 missing_periods 中。
    """
    rules = rule_set.freeze_rules
    by_period = {entry["period"]: entry for entry in frozen["periods"]}
    effective = [by_period[period] for period in rule_set.expected_periods if period in by_period]
    missing = [period for period in rule_set.expected_periods if period not in by_period]
    period_rows: list[dict[str, object]] = []
    failed: list[str] = []
    for entry in effective:
        passed = _period_passes(entry["value"], rule_set)
        if not passed:
            failed.append(entry["period"])
        period_rows.append({
            "period": entry["period"],
            "value": format(entry["value"], "f"),
            "source_id": entry["source_id"],
            "revision": entry["revision"],
            "passed": passed,
        })
    if len(effective) < rules.min_periods:
        conclusion = CONCLUSION_INSUFFICIENT
    elif len(failed) > rules.max_failed_periods:
        conclusion = CONCLUSION_REJECT
    else:
        conclusion = CONCLUSION_PASS
    return {
        "sample_id": sample_id,
        "conclusion": conclusion,
        "effective_periods": len(effective),
        "periods": period_rows,
        "missing_periods": missing,
        "failed_periods": failed,
        "conflicts": frozen["conflicts"],
        "superseded_records": frozen["superseded_records"],
        "retracted_records": frozen["retracted_records"],
    }


def analyze_batch(
    rule_set: RuleSet,
    rule_set_sha256: str,
    sample_ids: Sequence[str],
    records: Iterable[SeriesRecord],
) -> dict[str, object]:
    """先归并每个样本的结论，再按样本总体计算通过、拒绝和证据不足比例。"""
    if not sample_ids:
        raise ValueError("样本清单不能为空")
    by_sample: dict[str, list[SeriesRecord]] = {sample_id: [] for sample_id in sample_ids}
    for record in records:
        bucket = by_sample.get(record.sample_id)
        if bucket is not None:
            bucket.append(record)
    samples = [
        conclude_sample(
            sample_id,
            rule_set,
            freeze_sample_series(by_sample[sample_id], rule_set.freeze_rules.source_priority),
        )
        for sample_id in sample_ids
    ]
    total = len(samples)
    conclusions = {name: sum(1 for item in samples if item["conclusion"] == name) for name in CONCLUSIONS}
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "rule_set": {
            "rule_set_id": rule_set.rule_set_id,
            "version": rule_set.version,
            "sha256": rule_set_sha256,
        },
        "sample_count": total,
        "conclusions": conclusions,
        "rates": {name: _rate(conclusions[name], total) for name in CONCLUSIONS},
        "samples": samples,
    }
