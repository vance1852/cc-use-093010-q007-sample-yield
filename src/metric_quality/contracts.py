"""统计质量规则集与观测记录的严格数据契约。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence


class ValidationError(ValueError):
    """输入不能满足领域契约。"""


def _require_mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{path} 必须是对象")
    return value


def _require_sequence(value: object, path: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValidationError(f"{path} 必须是数组")
    return value


def _required_text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{path} 必须是非空字符串")
    return value.strip()


def _decimal(value: object, path: str) -> Decimal:
    if isinstance(value, bool):
        raise ValidationError(f"{path} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"{path} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationError(f"{path} 必须是有限数值")
    return result


def _int(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{path} 必须是整数")
    return value


@dataclass(frozen=True, slots=True)
class Indicator:
    """被考核的单一统计指标及其达标阈值。"""

    key: str
    label: str
    direction: str
    threshold: Decimal

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "Indicator":
        data = _require_mapping(raw, path)
        direction = _required_text(data.get("direction"), f"{path}.direction")
        if direction not in {"higher", "lower"}:
            raise ValidationError(f"{path}.direction 必须是 higher 或 lower")
        return cls(
            key=_required_text(data.get("key"), f"{path}.key"),
            label=_required_text(data.get("label"), f"{path}.label"),
            direction=direction,
            threshold=_decimal(data.get("threshold"), f"{path}.threshold"),
        )


@dataclass(frozen=True, slots=True)
class FreezeRules:
    """把同一批观测序列归并为样本结论的冻结规则。"""

    min_periods: int
    max_failed_periods: int
    source_priority: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: object, path: str, period_count: int) -> "FreezeRules":
        data = _require_mapping(raw, path)
        min_periods = _int(data.get("min_periods"), f"{path}.min_periods")
        if min_periods < 1 or min_periods > period_count:
            raise ValidationError(f"{path}.min_periods 必须在 1 到 {period_count} 之间")
        max_failed = _int(data.get("max_failed_periods"), f"{path}.max_failed_periods")
        if max_failed < 0:
            raise ValidationError(f"{path}.max_failed_periods 不能为负")
        if max_failed >= min_periods:
            raise ValidationError(f"{path}.max_failed_periods 必须小于 min_periods，否则全部未达标的样本也会被判通过")
        priority = tuple(
            _required_text(item, f"{path}.source_priority[{index}]")
            for index, item in enumerate(
                _require_sequence(data.get("source_priority"), f"{path}.source_priority")
            )
        )
        if not priority:
            raise ValidationError(f"{path}.source_priority 不能为空")
        if len(set(priority)) != len(priority):
            raise ValidationError(f"{path}.source_priority 不能重复")
        return cls(min_periods=min_periods, max_failed_periods=max_failed, source_priority=priority)


@dataclass(frozen=True, slots=True)
class RuleSet:
    """一次发布即不可变的统计质量规则版本。"""

    rule_set_id: str
    version: int
    title: str
    indicator: Indicator
    expected_periods: tuple[str, ...]
    freeze_rules: FreezeRules

    @classmethod
    def from_dict(cls, raw: object) -> "RuleSet":
        data = _require_mapping(raw, "rule_set")
        version = _int(data.get("version"), "rule_set.version")
        if version <= 0:
            raise ValidationError("rule_set.version 必须是正整数")
        periods = tuple(
            _required_text(item, f"rule_set.expected_periods[{index}]")
            for index, item in enumerate(
                _require_sequence(data.get("expected_periods"), "rule_set.expected_periods")
            )
        )
        if not periods:
            raise ValidationError("rule_set.expected_periods 不能为空")
        if len(set(periods)) != len(periods):
            raise ValidationError("rule_set.expected_periods 不能重复")
        return cls(
            rule_set_id=_required_text(data.get("rule_set_id"), "rule_set.rule_set_id"),
            version=version,
            title=_required_text(data.get("title"), "rule_set.title"),
            indicator=Indicator.from_dict(data.get("indicator"), "rule_set.indicator"),
            expected_periods=periods,
            freeze_rules=FreezeRules.from_dict(data.get("freeze_rules"), "rule_set.freeze_rules", len(periods)),
        )


@dataclass(frozen=True, slots=True)
class ObservationInput:
    """一条待导入的观测记录；复报递增版本号，撤销以 kind=retraction 表示。"""

    source_id: str
    sample_id: str
    period: str
    revision: int
    kind: str
    value: Decimal | None

    @classmethod
    def from_dict(cls, raw: object, path: str = "observation") -> "ObservationInput":
        data = _require_mapping(raw, path)
        revision = _int(data.get("revision"), f"{path}.revision")
        if revision < 1:
            raise ValidationError(f"{path}.revision 必须是正整数")
        kind = _required_text(data.get("kind"), f"{path}.kind")
        if kind not in {"report", "retraction"}:
            raise ValidationError(f"{path}.kind 必须是 report 或 retraction")
        raw_value = data.get("value")
        value: Decimal | None
        if kind == "report":
            if raw_value is None:
                raise ValidationError(f"{path}.value 对 report 记录不能为空")
            value = _decimal(raw_value, f"{path}.value")
        else:
            if raw_value is not None:
                raise ValidationError(f"{path}.value 对 retraction 记录必须为空")
            value = None
        return cls(
            source_id=_required_text(data.get("source_id"), f"{path}.source_id"),
            sample_id=_required_text(data.get("sample_id"), f"{path}.sample_id"),
            period=_required_text(data.get("period"), f"{path}.period"),
            revision=revision,
            kind=kind,
            value=value,
        )
