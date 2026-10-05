"""분석 일정(ANA-04.01): {preset: DAILY|WEEKLY, at, weekday?} 또는 {cron}. 다음 실행 시각은 조직 시간대로 계산한다.

cron은 5필드(분 시 일 월 요일) 또는 Spring 형식 6필드(초 분 시 일 월 요일)를 받는다. 잘못된 식은 ANALYSIS_SCHEDULE_INVALID(400).
일정 실행 자체는 core-api 스케줄러(ShedLock)가 API-ANA-33 trigger=SCHEDULE로 요청한다(TC-ANA-096). 여기서는 검증과 다음 시각 계산만 한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..common.errors import BusinessError, ErrorCode

DOW = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6}
MON = {m: i + 1 for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


def _invalid() -> BusinessError:
    return BusinessError(ErrorCode.ANALYSIS_SCHEDULE_INVALID)


def _field(expr: str, lo: int, hi: int, names: dict[str, int] | None = None) -> set[int]:
    out: set[int] = set()
    for part in expr.split(","):
        part = part.strip().upper()
        if not part:
            raise _invalid()
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            if not step_s.isdigit() or int(step_s) <= 0:
                raise _invalid()
            step = int(step_s)
        if part in ("*", "?"):
            a, b = lo, hi
        elif "-" in part:
            a_s, b_s = part.split("-", 1)
            a, b = _value(a_s, names), _value(b_s, names)
        else:
            a = _value(part, names)
            b = hi if step > 1 else a
        if not (lo <= a <= hi and lo <= b <= hi) or a > b:
            raise _invalid()
        out.update(range(a, b + 1, step))
    return out


def _value(token: str, names: dict[str, int] | None) -> int:
    if names and token in names:
        return names[token]
    if not token.isdigit():
        raise _invalid()
    return int(token)


class Cron:
    def __init__(self, expr: str):
        parts = expr.split()
        if len(parts) == 5:
            parts = ["0", *parts]
        if len(parts) != 6:
            raise _invalid()
        self.seconds = _field(parts[0], 0, 59)
        self.minutes = _field(parts[1], 0, 59)
        self.hours = _field(parts[2], 0, 23)
        self.days = _field(parts[3], 1, 31)
        self.months = _field(parts[4], 1, 12, MON)
        dows = _field(parts[5], 0, 7, DOW)
        self.dows = {d % 7 for d in dows}
        self.dom_any = parts[3] in ("*", "?")
        self.dow_any = parts[5] in ("*", "?")

    def _day_ok(self, d: datetime) -> bool:
        dow = (d.weekday() + 1) % 7  # 일=0
        dom_ok = d.day in self.days
        dow_ok = dow in self.dows
        if self.dom_any and self.dow_any:
            return True
        if self.dom_any:
            return dow_ok
        if self.dow_any:
            return dom_ok
        return dom_ok or dow_ok

    def next_after(self, after: datetime) -> datetime:
        t = after.replace(microsecond=0) + timedelta(seconds=1)
        limit = after + timedelta(days=366 * 4)
        while t <= limit:
            if t.month not in self.months:
                t = (t.replace(day=1, hour=0, minute=0, second=0) + timedelta(days=32)).replace(day=1)
                continue
            if not self._day_ok(t):
                t = (t + timedelta(days=1)).replace(hour=0, minute=0, second=0)
                continue
            if t.hour not in self.hours:
                t = (t + timedelta(hours=1)).replace(minute=0, second=0)
                continue
            if t.minute not in self.minutes:
                t = (t + timedelta(minutes=1)).replace(second=0)
                continue
            if t.second not in self.seconds:
                t = t + timedelta(seconds=1)
                continue
            return t
        raise _invalid()


def to_cron(schedule: dict) -> Cron:
    if not isinstance(schedule, dict):
        raise _invalid()
    if schedule.get("cron"):
        return Cron(str(schedule["cron"]))
    preset = schedule.get("preset")
    at = str(schedule.get("at", "00:00"))
    try:
        hh, mm = (int(x) for x in at.split(":"))
    except ValueError as exc:
        raise _invalid() from exc
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise _invalid()
    if preset == "DAILY":
        return Cron(f"0 {mm} {hh} * * *")
    if preset == "WEEKLY":
        weekday = str(schedule.get("weekday", "MON")).upper()
        if weekday.isdigit():
            weekday = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"][int(weekday) % 8]
        if weekday not in DOW:
            raise _invalid()
        return Cron(f"0 {mm} {hh} * * {weekday}")
    raise _invalid()


def validate(schedule: dict | None) -> None:
    if schedule:
        to_cron(schedule)


def next_run(schedule: dict | None, now: datetime, tz: str = "Asia/Seoul") -> datetime | None:
    if not schedule:
        return None
    zone = ZoneInfo(schedule.get("timezone") or tz)
    local = now.astimezone(zone).replace(tzinfo=None)
    nxt = to_cron(schedule).next_after(local)
    return nxt.replace(tzinfo=zone).astimezone(ZoneInfo("UTC"))
