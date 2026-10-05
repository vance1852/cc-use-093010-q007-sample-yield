"""统计资料质量的纯计算核心。

明确区分三个身份层次：

- 报送主体（reporter）：提交数据的经济体/机构；
- 样本（sample）：被观测的统计单位，归属于唯一报送主体；
- 观测序列（series）：一个样本针对同一指标在多个报告期形成的记录序列。

分析分两步：先按冻结规则把每个样本的整条序列归并为唯一结论
（``pass``/``reject``/``insufficient``），再以样本为分母计算
通过、拒绝和证据不足比例。达标观测点不再被当作合格样本，
因此通过率不可能超过 1，三类比例之和恒为 1。

本模块无副作用、无 I/O，相同输入必然得到相同输出。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "ALGORITHM_VERSION",
    "QualityInputError",
    "FrozenPolicy",
    "ObservationRecord",
    "evaluate",
    "freeze_series",
]

#: 新质量模型的算法版本。旧的“按观测点计合格率”算法是另一个版本，
#: 其结论只能以旧版本原样保留，不能被本算法静默改写。
ALGORITHM_VERSION = "metric-quality-sample-freeze/2"

VALUE_KINDS = ("report", "resubmission")
MARKER_KINDS = ("missing", "withdrawal")
RECORD_KINDS = VALUE_KINDS + MARKER_KINDS
CONCLUSIONS = ("pass", "reject", "insufficient")

# 同一时间戳下的确定性先后次序：行政性标记视为最后到达，
# 复报视为晚于初次报送。只在 recorded_at 完全相同时启用。
_KIND_RANK = {"report": 1, "resubmission": 2, "missing": 3, "withdrawal": 4}
_RATE_QUANTUM = Decimal("0.000001")


class QualityInputError(ValueError):
    """输入不满足质量模型契约，消息可直接展示给分析员。"""


def _text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QualityInputError(f"{path} 必须是非空字符串")
    return value.strip()


def _decimal(value: object, path: str) -> Decimal:
    if isinstance(value, bool):
        raise QualityInputError(f"{path} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise QualityInputError(f"{path} 必须是十进制数值") from exc
    if not result.is_finite():
        raise QualityInputError(f"{path} 必须是有限数值")
    return result


def _iso_timestamp(value: object, path: str) -> str:
    text = _text(value, path)
    try:
        datetime.fromisoformat(text)
    except ValueError as exc:
        raise QualityInputError(f"{path} 必须是 ISO 8601 时间戳") from exc
    return text


@dataclass(frozen=True, slots=True)
class FrozenPolicy:
    """一次冻结所依据的不可变规则版本。"""

    policy_id: str
    version: int
    indicator_id: str
    reporter_ids: tuple[str, ...]
    periods: tuple[str, ...]
    source_priority: tuple[str, ...]
    operator: str
    threshold: Decimal
    conflict_tolerance: Decimal = Decimal(0)
    require_all_periods: bool = True
    supersedes_version: int | None = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "FrozenPolicy":
        if not isinstance(raw, Mapping):
            raise QualityInputError("规则必须是 JSON 对象")
        version = raw.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise QualityInputError("policy.version 必须是正整数")
        reporters = raw.get("reporter_ids")
        if not isinstance(reporters, Sequence) or isinstance(reporters, (str, bytes)):
            raise QualityInputError("policy.reporter_ids 必须是字符串数组")
        reporter_ids = tuple(_text(item, "policy.reporter_ids") for item in reporters)
        if not reporter_ids:
            raise QualityInputError("policy.reporter_ids 至少登记一个报送主体")
        if len(set(reporter_ids)) != len(reporter_ids):
            raise QualityInputError("policy.reporter_ids 不能有重复报送主体")
        periods = raw.get("periods")
        if not isinstance(periods, Sequence) or isinstance(periods, (str, bytes)):
            raise QualityInputError("policy.periods 必须是字符串数组")
        periods = tuple(_text(item, "policy.periods") for item in periods)
        if not periods:
            raise QualityInputError("policy.periods 至少声明一个报告期")
        if len(set(periods)) != len(periods):
            raise QualityInputError("policy.periods 不能有重复报告期")
        priority = raw.get("source_priority")
        if not isinstance(priority, Sequence) or isinstance(priority, (str, bytes)):
            raise QualityInputError("policy.source_priority 必须是字符串数组")
        source_priority = tuple(_text(item, "policy.source_priority") for item in priority)
        if not source_priority:
            raise QualityInputError("policy.source_priority 至少登记一个权威来源")
        if len(set(source_priority)) != len(source_priority):
            raise QualityInputError("policy.source_priority 不能有重复来源")
        operator = _text(raw.get("operator"), "policy.operator")
        if operator not in {"gte", "lte"}:
            raise QualityInputError("policy.operator 只能是 gte 或 lte")
        tolerance = _decimal(raw.get("conflict_tolerance", 0), "policy.conflict_tolerance")
        if tolerance < 0:
            raise QualityInputError("policy.conflict_tolerance 不能为负")
        require_all = raw.get("require_all_periods", True)
        if not isinstance(require_all, bool):
            raise QualityInputError("policy.require_all_periods 必须是布尔值")
        supersedes = raw.get("supersedes_version")
        if supersedes is not None:
            if isinstance(supersedes, bool) or not isinstance(supersedes, int) or supersedes <= 0:
                raise QualityInputError("policy.supersedes_version 必须是正整数")
            if supersedes >= version:
                raise QualityInputError("policy.supersedes_version 必须小于当前版本")
        return cls(
            policy_id=_text(raw.get("policy_id"), "policy.policy_id"),
            version=version,
            indicator_id=_text(raw.get("indicator_id"), "policy.indicator_id"),
            reporter_ids=reporter_ids,
            periods=periods,
            source_priority=source_priority,
            operator=operator,
            threshold=_decimal(raw.get("threshold"), "policy.threshold"),
            conflict_tolerance=tolerance,
            require_all_periods=require_all,
            supersedes_version=supersedes,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "indicator_id": self.indicator_id,
            "reporter_ids": list(self.reporter_ids),
            "periods": list(self.periods),
            "source_priority": list(self.source_priority),
            "operator": self.operator,
            "threshold": format(self.threshold, "f"),
            "conflict_tolerance": format(self.conflict_tolerance, "f"),
            "require_all_periods": self.require_all_periods,
            "supersedes_version": self.supersedes_version,
        }

    @property
    def source_rank(self) -> dict[str, int]:
        return {source: index for index, source in enumerate(self.source_priority)}


@dataclass(frozen=True, slots=True)
class ObservationRecord:
    """观测序列中的一条原始报送记录。"""

    reporter_id: str
    sample_id: str
    indicator_id: str
    period: str
    kind: str
    recorded_at: str
    value: Decimal | None = None
    source_id: str | None = None
    record_id: str | None = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ObservationRecord":
        if not isinstance(raw, Mapping):
            raise QualityInputError("观测记录必须是 JSON 对象")
        kind = _text(raw.get("kind"), "record.kind")
        if kind not in RECORD_KINDS:
            raise QualityInputError(f"record.kind 只能是 {('/'.join(RECORD_KINDS))}")
        value: Decimal | None
        source_id: str | None
        if kind in VALUE_KINDS:
            value = _decimal(raw.get("value"), "record.value")
            source_id = _text(raw.get("source_id"), "record.source_id")
        else:
            if raw.get("value") is not None:
                raise QualityInputError(f"{kind} 记录不能携带数值")
            value = None
            raw_source = raw.get("source_id")
            source_id = None if raw_source is None else _text(raw_source, "record.source_id")
        record_id = raw.get("record_id")
        if record_id is not None:
            record_id = _text(record_id, "record.record_id")
        return cls(
            reporter_id=_text(raw.get("reporter_id"), "record.reporter_id"),
            sample_id=_text(raw.get("sample_id"), "record.sample_id"),
            indicator_id=_text(raw.get("indicator_id"), "record.indicator_id"),
            period=_text(raw.get("period"), "record.period"),
            kind=kind,
            recorded_at=_iso_timestamp(raw.get("recorded_at"), "record.recorded_at"),
            value=value,
            source_id=source_id,
            record_id=record_id,
        )

    @property
    def order_key(self) -> tuple[str, int, str, str]:
        return (
            self.recorded_at,
            _KIND_RANK[self.kind],
            self.source_id or "",
            self.record_id or "",
        )


def _meets(policy: FrozenPolicy, value: Decimal) -> bool:
    return value >= policy.threshold if policy.operator == "gte" else value <= policy.threshold


def _freeze_period(
    policy: FrozenPolicy, period: str, records: Sequence[ObservationRecord]
) -> dict[str, Any]:
    """把同一报告期的记录归并为一个冻结值。"""

    ordered = sorted(records, key=lambda item: item.order_key)
    value_events = [item for item in ordered if item.kind in VALUE_KINDS]
    markers = [item for item in ordered if item.kind in MARKER_KINDS]
    if not value_events and not markers:
        # 声明的报告期没有任何记录：确定性地视为缺期。
        return {
            "period": period,
            "status": "missing",
            "value": None,
            "source_id": None,
            "record_kind": None,
            "recorded_at": None,
            "record_ids": [],
        }
    latest_value = value_events[-1] if value_events else None
    latest_marker = markers[-1] if markers else None

    # 撤销或缺期标记晚于最后一次数值报送时，该期没有有效值。
    if latest_marker is not None and (
        latest_value is None or latest_marker.order_key > latest_value.order_key
    ):
        return {
            "period": period,
            "status": "withdrawn" if latest_marker.kind == "withdrawal" else "missing",
            "value": None,
            "source_id": None,
            "record_kind": latest_marker.kind,
            "recorded_at": latest_marker.recorded_at,
            "record_ids": [item.record_id for item in ordered if item.record_id],
        }

    # 每个来源只保留其时间序上的最后一次报送；更早的同来源记录被复报取代。
    by_source: dict[str, ObservationRecord] = {}
    superseded: list[str] = []
    for item in value_events:
        previous = by_source.get(item.source_id or "")
        if previous is not None and previous.record_id:
            superseded.append(previous.record_id)
        by_source[item.source_id or ""] = item

    candidates = sorted(
        by_source.values(),
        key=lambda item: (policy.source_rank.get(item.source_id or "", len(policy.source_priority)), item.source_id or ""),
    )
    values = [item.value for item in candidates if item.value is not None]
    spread = max(values) - min(values) if values else Decimal(0)
    if spread > policy.conflict_tolerance:
        return {
            "period": period,
            "status": "conflict",
            "value": None,
            "source_id": None,
            "record_kind": None,
            "recorded_at": candidates[-1].recorded_at,
            "conflicting_sources": [
                {"source_id": item.source_id, "value": format(item.value, "f"), "recorded_at": item.recorded_at}
                for item in candidates
            ],
            "record_ids": [item.record_id for item in ordered if item.record_id],
        }

    chosen = candidates[0]
    return {
        "period": period,
        "status": "reported",
        "value": format(chosen.value, "f"),
        "source_id": chosen.source_id,
        "record_kind": chosen.kind,
        "recorded_at": chosen.recorded_at,
        "superseded_record_ids": superseded,
        "record_ids": [item.record_id for item in ordered if item.record_id],
    }


def freeze_series(
    policy: FrozenPolicy, sample_id: str, reporter_id: str, records: Sequence[ObservationRecord]
) -> dict[str, Any]:
    """归并单个样本的整条观测序列，给出唯一结论与可追溯理由。"""

    grouped: dict[str, list[ObservationRecord]] = {period: [] for period in policy.periods}
    for item in records:
        if item.period not in grouped:
            raise QualityInputError(f"样本 {sample_id} 含未在规则中声明的报告期: {item.period}")
        grouped[item.period].append(item)

    periods = [_freeze_period(policy, period, grouped[period]) for period in policy.periods]
    reasons: list[str] = []
    for frozen in periods:
        if frozen["status"] == "conflict":
            details = ", ".join(
                f"{entry['source_id']}={entry['value']}" for entry in frozen["conflicting_sources"]
            )
            reasons.append(f"conflict:{frozen['period']}({details})")
        elif frozen["status"] == "missing":
            reasons.append(f"missing_period:{frozen['period']}")
        elif frozen["status"] == "withdrawn":
            reasons.append(f"withdrawn_period:{frozen['period']}")

    if reasons and policy.require_all_periods:
        conclusion = "insufficient"
    elif any(
        frozen["status"] == "conflict" for frozen in periods
    ):
        # 即使不要求全期覆盖，冲突期也无法形成结论。
        conclusion = "insufficient"
    else:
        reported = [frozen for frozen in periods if frozen["status"] == "reported"]
        if not reported:
            conclusion = "insufficient"
            reasons.append("no_reported_period")
        elif any(not _meets(policy, Decimal(frozen["value"])) for frozen in reported):
            conclusion = "reject"
            for frozen in reported:
                if not _meets(policy, Decimal(frozen["value"])):
                    reasons.append(f"threshold_failed:{frozen['period']}={frozen['value']}")
        else:
            conclusion = "pass"

    return {
        "sample_id": sample_id,
        "reporter_id": reporter_id,
        "indicator_id": policy.indicator_id,
        "conclusion": conclusion,
        "reasons": reasons,
        "periods": periods,
        "record_count": sum(len(group) for group in grouped.values()),
    }


def _rates(counts: Mapping[str, int], total: int) -> dict[str, str]:
    pass_rate = (Decimal(counts["pass"]) / Decimal(total)).quantize(_RATE_QUANTUM)
    reject_rate = (Decimal(counts["reject"]) / Decimal(total)).quantize(_RATE_QUANTUM)
    # 证据不足率取差值，保证三类比例之和严格等于 1。
    insufficient_rate = Decimal(1) - pass_rate - reject_rate
    return {
        "pass": format(pass_rate, "f"),
        "reject": format(reject_rate, "f"),
        "insufficient": format(insufficient_rate, "f"),
    }


def evaluate(
    policy: FrozenPolicy, records: Iterable[ObservationRecord]
) -> dict[str, Any]:
    """按冻结规则评估全部样本，返回以样本为分母的质量结论。"""

    all_records = tuple(records)
    if not all_records:
        raise QualityInputError("没有可冻结的观测记录")
    samples: dict[str, list[ObservationRecord]] = {}
    reporter_of: dict[str, str] = {}
    for item in all_records:
        if item.indicator_id != policy.indicator_id:
            raise QualityInputError(
                f"记录指标 {item.indicator_id} 与规则指标 {policy.indicator_id} 不一致"
            )
        if item.reporter_id not in policy.reporter_ids:
            raise QualityInputError(
                f"报送主体 {item.reporter_id} 未在规则中登记（样本 {item.sample_id}）"
            )
        registered_source = item.source_id
        if registered_source is not None and registered_source not in policy.source_rank:
            raise QualityInputError(f"来源 {registered_source} 未在规则 source_priority 中登记")
        prior_reporter = reporter_of.get(item.sample_id)
        if prior_reporter is not None and prior_reporter != item.reporter_id:
            raise QualityInputError(
                f"样本 {item.sample_id} 同时归属报送主体 {prior_reporter} 和 {item.reporter_id}"
            )
        reporter_of[item.sample_id] = item.reporter_id
        samples.setdefault(item.sample_id, []).append(item)

    sample_results = [
        freeze_series(policy, sample_id, reporter_of[sample_id], sample_records)
        for sample_id, sample_records in sorted(samples.items())
    ]
    counts = {name: 0 for name in CONCLUSIONS}
    for result in sample_results:
        counts[result["conclusion"]] += 1

    by_reporter: dict[str, dict[str, Any]] = {}
    for result in sample_results:
        bucket = by_reporter.setdefault(
            result["reporter_id"], {"sample_count": 0, "counts": {name: 0 for name in CONCLUSIONS}}
        )
        bucket["sample_count"] += 1
        bucket["counts"][result["conclusion"]] += 1
    reporter_breakdown = []
    for reporter_id in sorted(by_reporter):
        bucket = by_reporter[reporter_id]
        reporter_breakdown.append({
            "reporter_id": reporter_id,
            "sample_count": bucket["sample_count"],
            "counts": bucket["counts"],
            "rates": _rates(bucket["counts"], bucket["sample_count"]),
        })

    total = len(sample_results)
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "policy": policy.to_dict(),
        "sample_count": total,
        "reporter_count": len(by_reporter),
        "record_count": len(all_records),
        "counts": counts,
        "rates": _rates(counts, total),
        "by_reporter": reporter_breakdown,
        "samples": sample_results,
    }


def field_canonical_tuple(record: ObservationRecord) -> tuple[Any, ...]:
    """记录的规范化身份，用于计算冻结输入摘要。"""

    return (
        record.record_id,
        record.reporter_id,
        record.sample_id,
        record.indicator_id,
        record.period,
        record.kind,
        None if record.value is None else format(record.value, "f"),
        record.source_id,
        record.recorded_at,
    )
