"""可替换的时间源，便于确定性测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FrozenClock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def now(self) -> datetime:
        return self.moment

    def advance(self, *, seconds: int = 0) -> None:
        self.moment += timedelta(seconds=seconds)


def isoformat(moment: datetime) -> str:
    return moment.isoformat()
