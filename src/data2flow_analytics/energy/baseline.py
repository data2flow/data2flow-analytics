"""기준선 학습(ENE-01.04, BR-ENE-08). DB와 떨어진 순수 함수다.

- 도일(degree day): 일 평균 기온(사이트 시간대 00~24시 시간 평균) t에서 CDD = max(0, t − 냉방 기준), HDD = max(0, 난방 기준 − t)
- 회귀(DEGREE_DAY_REGRESSION): 일 kWh = a + b·CDD + c·HDD (+ d·주말) 최소제곱, R²를 기록하고 R² < 0.5면 LOW_FIT 경고
- 기간 평균(PERIOD_AVERAGE): 평일 평균(절편) + 주말 차이. 기온이 필요 없다
- 30일 미만이면 InsufficientData(needed_days). 가상(시뮬레이션) 공간 행은 학습에서 뺀다
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np

MIN_DAYS = 30
LOW_FIT_R2 = 0.5
COOLING_BASE_C = 24.0
HEATING_BASE_C = 18.0
METHODS = ("PERIOD_AVERAGE", "DEGREE_DAY_REGRESSION")
COEFFICIENTS = ("intercept", "cdd", "hdd", "weekend")


class InsufficientData(Exception):  # noqa: N818 — 명세의 이름(TC-ENE-024)
    def __init__(self, needed_days: int, have_days: int):
        super().__init__(f"{MIN_DAYS}일 이상 필요합니다(현재 {have_days}일)")
        self.needed_days = needed_days
        self.have_days = have_days


@dataclass(frozen=True)
class Day:
    day: date
    kwh: float
    cdd: float = 0.0
    hdd: float = 0.0
    weekend: bool = False
    virtual: bool = False


@dataclass
class Model:
    method: str
    train_from: date
    train_to: date
    coefficients: dict[str, float]
    r2: float | None
    residuals: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    days: int = 0

    def predict(self, cdd: float = 0.0, hdd: float = 0.0, weekend: bool = False) -> float:
        c = self.coefficients
        return c["intercept"] + c["cdd"] * cdd + c["hdd"] * hdd + (c["weekend"] if weekend else 0.0)

    def to_json(self) -> dict:
        return {"method": self.method, "trainFrom": self.train_from.isoformat(), "trainTo": self.train_to.isoformat(),
                "coefficients": self.coefficients, "r2": self.r2, "warnings": self.warnings, "days": self.days}


def degree_days(mean_c: float, cooling_base: float = COOLING_BASE_C, heating_base: float = HEATING_BASE_C) -> tuple[float, float]:
    """(CDD, HDD)"""
    return max(0.0, mean_c - cooling_base), max(0.0, heating_base - mean_c)


def daily_means(hourly: Iterable[tuple[datetime, float]], tz: str) -> dict[date, float]:
    """시간별 기온 → 사이트 시간대 날짜별 평균"""
    zone = ZoneInfo(tz)
    acc: dict[date, list[float]] = defaultdict(list)
    for at, value in hourly:
        if value is not None:
            acc[at.astimezone(zone).date()].append(float(value))
    return {d: sum(v) / len(v) for d, v in acc.items() if v}


def _r2(actual: np.ndarray, predicted: np.ndarray) -> float:
    ss_res = float(np.sum((actual - predicted) ** 2))
    ss_tot = float(np.sum((actual - actual.mean()) ** 2))
    if ss_tot == 0:
        return 1.0 if ss_res == 0 else 0.0
    return 1 - ss_res / ss_tot


def fit(days: Sequence[Day], method: str, *, split_weekday: bool = True) -> Model:
    """학습. 가상 공간 행은 빼고, 30일 미만이면 InsufficientData"""
    if method not in METHODS:
        raise ValueError(method)
    rows = sorted((d for d in days if not d.virtual), key=lambda d: d.day)
    if len(rows) < MIN_DAYS:
        raise InsufficientData(MIN_DAYS - len(rows), len(rows))
    y = np.array([d.kwh for d in rows], dtype=float)
    weekend = np.array([1.0 if (split_weekday and d.weekend) else 0.0 for d in rows])
    if method == "PERIOD_AVERAGE":
        weekday_mean = float(y[weekend == 0].mean()) if (weekend == 0).any() else float(y.mean())
        weekend_diff = float(y[weekend == 1].mean()) - weekday_mean if (weekend == 1).any() else 0.0
        coef = {"intercept": weekday_mean, "cdd": 0.0, "hdd": 0.0, "weekend": weekend_diff}
    else:
        columns = [np.ones(len(rows)), np.array([d.cdd for d in rows]), np.array([d.hdd for d in rows])]
        names = ["intercept", "cdd", "hdd"]
        if split_weekday:
            columns.append(weekend)
            names.append("weekend")
        x = np.column_stack(columns)
        keep = [i for i in range(x.shape[1]) if i == 0 or np.any(x[:, i] != 0)]  # 값이 모두 0인 열(예: 여름의 HDD)은 0으로 둔다
        beta, *_ = np.linalg.lstsq(x[:, keep], y, rcond=None)
        coef = dict.fromkeys(COEFFICIENTS, 0.0)
        for k, b in zip(keep, beta, strict=True):
            coef[names[k]] = float(b)
    model = Model(method, rows[0].day, rows[-1].day, coef, None, days=len(rows))
    predicted = np.array([model.predict(d.cdd, d.hdd, bool(split_weekday and d.weekend)) for d in rows])
    model.r2 = round(_r2(y, predicted), 4)
    model.residuals = [{"date": d.day.isoformat(), "actual": round(d.kwh, 3), "predicted": round(float(p), 3)} for d, p in zip(rows, predicted, strict=True)]
    if method == "DEGREE_DAY_REGRESSION" and model.r2 < LOW_FIT_R2:
        model.warnings.append("LOW_FIT")
    return model


def normalized_diff(actual_kwh: float, predicted_kwh: float) -> float | None:
    """기준선 대비 증감 비율(날씨 보정 후)"""
    return None if predicted_kwh <= 0 else (actual_kwh - predicted_kwh) / predicted_kwh
