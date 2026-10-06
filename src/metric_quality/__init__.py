"""数字贸易统计样本质量与审批服务。"""

from .analytics import ALGORITHM_VERSION, SeriesRecord, analyze_batch
from .contracts import FreezeRules, Indicator, ObservationInput, RuleSet, ValidationError
from .service import MetricQualityService

__all__ = [
    "ALGORITHM_VERSION",
    "FreezeRules",
    "Indicator",
    "MetricQualityService",
    "ObservationInput",
    "RuleSet",
    "SeriesRecord",
    "ValidationError",
    "analyze_batch",
]

__version__ = "0.2.0"
