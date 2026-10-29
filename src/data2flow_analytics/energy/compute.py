"""에너지 계산 규칙(ENE-01.01·01.02, BR-ENE-01~06). DB·이벤트와 떨어진 순수 함수다.

- 적분(BR-ENE-03): `power`(W)는 이웃 표본 사이 사다리꼴 적분, 수신 간격이 15분을 넘는 구간은 추정. 누적 `energy`(kWh)는 차분하고 감소(리셋)는 0
- 추정(BR-ENE-04): 정격 전력 × 가동 시간 + 대기 전력 × 비가동 시간
- 출처(BR-ENE-01): 측정이 있으면 합계는 측정, 추정은 비교용
- 비용·탄소(BR-ENE-05): 그 시각(현지 시간)이 속한 기간의 단가·계수. 월 기본요금은 월 단위 범위에만
- EUI(BR-ENE-06): 기간 사용량 / 면적. 면적이 없으면 None과 사유
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

GAP = timedelta(minutes=15)
METERED, ESTIMATED = "METERED", "ESTIMATED"
WEEKDAY, WEEKEND_HOLIDAY = "WEEKDAY", "WEEKEND_HOLIDAY"


@dataclass(frozen=True)
class Interval:
    """[start, end) 구간의 사용량. estimated는 수신 공백 등으로 값을 짐작한 구간"""

    start: datetime
    end: datetime
    kwh: float
    estimated: bool = False


def integrate_power(samples: Sequence[tuple[datetime, float]], gap: timedelta = GAP) -> list[Interval]:
    """전력(W) 표본의 사다리꼴 적분. 표본은 시각 순이어야 한다"""
    out = []
    for (t0, w0), (t1, w1) in zip(samples, samples[1:], strict=False):
        dt = t1 - t0
        if dt.total_seconds() <= 0:
            continue
        out.append(Interval(t0, t1, (w0 + w1) / 2 * dt.total_seconds() / 3600 / 1000, dt > gap))
    return out


def diff_cumulative(samples: Sequence[tuple[datetime, float]], gap: timedelta = GAP) -> list[Interval]:
    """누적 전력량(kWh) 차분. 값이 줄면 계량기 리셋으로 보고 그 구간은 0"""
    out = []
    for (t0, v0), (t1, v1) in zip(samples, samples[1:], strict=False):
        if t1 <= t0:
            continue
        out.append(Interval(t0, t1, max(0.0, v1 - v0), t1 - t0 > gap))
    return out


def total(intervals: Iterable[Interval]) -> tuple[float, bool]:
    """합계와 추정 구간 포함 여부"""
    kwh, estimated = 0.0, False
    for i in intervals:
        kwh += i.kwh
        estimated |= i.estimated
    return kwh, estimated


def clip_hourly(intervals: Iterable[Interval], start: datetime, end: datetime) -> list[Interval]:
    """구간들을 [start, end) 안의 1시간 칸으로 나눈다. 칸 경계를 넘는 구간은 겹친 시간 비율로 나누고, 추정 표시는 이어받는다.
    사용량이 있는 칸만 시각 순으로 돌려준다(날짜 경계 밖 표본이 걸친 구간도 날짜 안 몫만 센다)"""
    hours: dict[datetime, list] = {}
    for i in intervals:
        span = (i.end - i.start).total_seconds()
        a, b = max(i.start, start), min(i.end, end)
        if span <= 0 or b <= a:
            continue
        cursor = a
        while cursor < b:
            slot = start + timedelta(hours=int((cursor - start).total_seconds() // 3600))
            nxt = min(b, slot + timedelta(hours=1))
            acc = hours.setdefault(slot, [0.0, False])
            acc[0] += i.kwh * (nxt - cursor).total_seconds() / span
            acc[1] |= i.estimated
            cursor = nxt
    return [Interval(h, h + timedelta(hours=1), v[0], v[1]) for h, v in sorted(hours.items())]


def estimated_usage(rated_power_w: float, standby_power_w: float | None, running: Iterable[tuple[datetime, datetime]],
                    start: datetime, end: datetime) -> float:
    """기간 [start, end)의 추정 사용량(kWh). running은 가동 구간(ACT-08.02), 기간 밖은 잘라 낸다"""
    run = 0.0
    for a, b in _merge(running):
        a, b = max(a, start), min(b, end)
        if b > a:
            run += (b - a).total_seconds()
    span = max(0.0, (end - start).total_seconds())
    run = min(run, span)
    return (rated_power_w * run + (standby_power_w or 0) * (span - run)) / 3600 / 1000


def _merge(ranges: Iterable[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[list[datetime]] = []
    for a, b in sorted(ranges):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


@dataclass(frozen=True)
class SpaceUsage:
    kwh: float | None
    source: str | None
    comparison: float | None = None


def pick_source(metered: float | None, estimated: float | None) -> SpaceUsage:
    """같은 공간의 측정·추정 합계. 측정이 있으면 측정, 추정은 comparison으로만"""
    if metered is not None:
        return SpaceUsage(metered, METERED, estimated)
    if estimated is not None:
        return SpaceUsage(estimated, ESTIMATED)
    return SpaceUsage(None, None)


@dataclass(frozen=True)
class Tariff:
    valid_from: date
    mode: str
    flat_rate: float | None = None
    tou: dict | None = None
    base_charge: float | None = None
    scope_space_id: int | None = None

    @staticmethod
    def of(row: dict) -> Tariff:
        """API-ENE-70·API-ENE-01 응답 한 행"""
        flat = row.get("flatRateKrwPerKwh")
        base = row.get("baseChargeKrwPerMonth")
        scope = (row.get("scopeSpace") or {}).get("id")
        return Tariff(date.fromisoformat(row["validFrom"]), row["mode"], None if flat is None else float(flat), row.get("touSchedule"),
                      None if base is None else float(base), None if scope is None else int(scope))


@dataclass
class Tariffs:
    """조직·사이트 단가표. 사이트 단가가 그 날짜에 있으면 사이트, 없으면 조직 단가를 쓴다"""

    rows: Sequence[Tariff]
    holidays: frozenset[date] = field(default_factory=frozenset)

    def at(self, day: date, site_id: int | None = None) -> Tariff | None:
        for scope in ((site_id, None) if site_id is not None else (None,)):
            valid = [t for t in self.rows if t.scope_space_id == scope and t.valid_from <= day]
            if valid:
                return max(valid, key=lambda t: t.valid_from)
        return None

    def rate(self, local: datetime, site_id: int | None = None) -> float | None:
        """그 현지 시각의 kWh당 단가(원)"""
        tariff = self.at(local.date(), site_id)
        if tariff is None:
            return None
        if tariff.mode == "FLAT":
            return tariff.flat_rate
        day_type = WEEKEND_HOLIDAY if local.weekday() >= 5 or local.date() in self.holidays else WEEKDAY
        minute = local.hour * 60 + local.minute
        for season in (tariff.tou or {}).get("seasons", []):
            if local.month not in season.get("months", []):
                continue
            for band in season.get("bands", []):
                if band.get("dayType") == day_type and _minutes(band["from"]) <= minute < _minutes(band["to"]):
                    return float(band["rate"])
        return None


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


@dataclass(frozen=True)
class Cost:
    krw: float | None
    unpriced_kwh: float
    base_charge_krw: float


def cost(usage: Iterable[tuple[datetime, float]], tariffs: Tariffs, tz: str, *, site_id: int | None = None,
         months: Iterable[date] = ()) -> Cost:
    """시간대별 사용량(구간 시작 시각, kWh)의 비용. months를 주면(월 단위 범위) 그 달들의 기본요금을 더한다.
    단가가 없는 시각의 사용량은 unpriced_kwh로 따로 센다(비용에 넣지 않는다)"""
    zone = ZoneInfo(tz)
    krw, unpriced, priced = 0.0, 0.0, False
    for at, kwh in usage:
        rate = tariffs.rate(at.astimezone(zone), site_id)
        if rate is None:
            unpriced += kwh
        else:
            krw += kwh * rate
            priced = True
    base = 0.0
    for month in months:
        tariff = tariffs.at(month.replace(day=1), site_id)
        base += (tariff.base_charge or 0) if tariff else 0
    return Cost(krw + base if priced or base else None, unpriced, base)


@dataclass(frozen=True)
class EmissionFactor:
    valid_from: date
    factor_tco2_per_mwh: float


def factor_at(factors: Sequence[EmissionFactor], day: date) -> float | None:
    valid = [f for f in factors if f.valid_from <= day]
    return max(valid, key=lambda f: f.valid_from).factor_tco2_per_mwh if valid else None


def carbon(daily: Iterable[tuple[date, float]], factors: Sequence[EmissionFactor]) -> float | None:
    """일별 사용량의 탄소(tCO2eq). 날짜마다 그 날짜가 속한 기간의 계수. 표시는 round_carbon"""
    out, any_ = 0.0, False
    for day, kwh in daily:
        f = factor_at(factors, day)
        if f is not None:
            out += kwh / 1000 * f
            any_ = True
    return out if any_ else None


def round_carbon(tco2: float | None) -> float | None:
    """화면·API 표시는 소수 둘째 자리(TC-ENE-010)"""
    return None if tco2 is None else round(tco2 + 1e-12, 2)


@dataclass(frozen=True)
class Intensity:
    value: float | None
    reason: str | None = None
    denominator_source: str | None = None


def eui(kwh: float, area_m2: float | None) -> Intensity:
    """면적당 사용량(kWh/㎡). 면적이 없거나 0이면 계산하지 않는다"""
    if area_m2 is None or area_m2 <= 0:
        return Intensity(None, "NO_AREA")
    return Intensity(kwh / area_m2)


def per_occupied_hour(kwh: float, occupied_hours: float | None, scheduled_hours: float | None) -> Intensity:
    """재실 시간당 사용량. 재실 추정(occupancy_est) 시간을 먼저, 없으면 운영 시간표 시간을 분모로 쓰고 출처를 붙인다"""
    if occupied_hours is not None and occupied_hours > 0:
        return Intensity(kwh / occupied_hours, None, "OCCUPANCY_EST")
    if scheduled_hours is not None and scheduled_hours > 0:
        return Intensity(kwh / scheduled_hours, None, "OPERATING_SCHEDULE")
    return Intensity(None, "NO_OCCUPANCY")
