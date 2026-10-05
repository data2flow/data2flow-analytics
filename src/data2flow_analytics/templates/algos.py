"""템플릿이 함께 쓰는 통계 도우미. 난수는 쓰지 않거나 호출자가 시드를 넘긴다(BR-ANA-10)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import stats

from .base import robust_sigma


def median_step(index: pd.DatetimeIndex) -> pd.Timedelta:
    if len(index) < 2:
        return pd.Timedelta(hours=1)
    diffs = np.diff(index.values).astype("timedelta64[ns]").astype(np.int64)
    return pd.Timedelta(int(np.median(diffs)), unit="ns")


def steps_for(duration: pd.Timedelta, step: pd.Timedelta, minimum: int = 1) -> int:
    return max(minimum, int(round(duration / step))) if step > pd.Timedelta(0) else minimum


def _slot_keys(index: pd.DatetimeIndex, tz: str, slot_minutes: int, weekly: bool) -> pd.MultiIndex:
    local = index.tz_convert(tz)
    minute_of_day = local.hour * 60 + local.minute
    slot = (minute_of_day // slot_minutes).astype(int)
    daytype = (local.dayofweek >= 5).astype(int) if weekly else np.zeros(len(index), dtype=int)
    return pd.MultiIndex.from_arrays([np.asarray(daytype), np.asarray(slot)], names=["daytype", "slot"])


@dataclass
class SeasonalProfile:
    """요일 유형(평일·주말) × 하루 시간대 칸의 로버스트 중앙값(일·주 주기 제거용)."""

    tz: str
    slot_minutes: int
    weekly: bool
    table: dict[tuple[int, int], float]
    fallback: float

    def baseline(self, index: pd.DatetimeIndex) -> np.ndarray:
        keys = _slot_keys(index, self.tz, self.slot_minutes, self.weekly)
        return np.array([self.table.get(k, self.fallback) for k in keys], dtype=float)

    def value_at(self, ts: pd.Timestamp) -> float:
        return float(self.baseline(pd.DatetimeIndex([ts]))[0])

    def to_json(self) -> dict:
        return {"tz": self.tz, "slotMinutes": self.slot_minutes, "weekly": self.weekly,
                "table": [[d, s, v] for (d, s), v in sorted(self.table.items())], "fallback": self.fallback}

    @staticmethod
    def from_json(data: dict) -> SeasonalProfile:
        return SeasonalProfile(tz=data["tz"], slot_minutes=int(data["slotMinutes"]), weekly=bool(data["weekly"]),
                               table={(int(d), int(s)): float(v) for d, s, v in data["table"]}, fallback=float(data["fallback"]))


def fit_profile(s: pd.Series, tz: str, period: str = "AUTO") -> SeasonalProfile:
    s = s.dropna()
    step = median_step(s.index)
    slot_minutes = int(max(15, min(60, step / pd.Timedelta(minutes=1))))
    if step >= pd.Timedelta(hours=1):
        slot_minutes = 60
    span_days = (s.index[-1] - s.index[0]) / pd.Timedelta(days=1) if len(s) > 1 else 0
    local = s.index.tz_convert(tz)
    n_weekend_days = len(set(local[local.dayofweek >= 5].date))
    n_weekday_days = len(set(local[local.dayofweek < 5].date))
    if period == "NONE" or step >= pd.Timedelta(days=1):
        fallback = float(np.nanmedian(s.values)) if len(s) else 0.0
        return SeasonalProfile(tz, 1440, False, {}, fallback)
    wants_weekly = period in ("WEEKLY", "AUTO")
    # 평일·주말이 각각 4일 이상이면 요일 유형별 칸 표, 1일 이상이면 하루 표 + 요일 유형별 차이(오프셋)
    table_mode = wants_weekly and n_weekend_days >= 4 and n_weekday_days >= 4
    offset_mode = wants_weekly and not table_mode and n_weekend_days >= 1 and n_weekday_days >= 1
    table = _slot_table(s, tz, slot_minutes, table_mode)
    if offset_mode:
        base = SeasonalProfile(tz, slot_minutes, False, table, float(np.nanmedian(s.values)))
        resid = s.to_numpy() - base.baseline(s.index)
        weekend = np.asarray(local.dayofweek >= 5)
        offsets = {0: float(np.median(resid[~weekend])), 1: float(np.median(resid[weekend]))}
        table = {(d, slot): v + offsets[d] for (_, slot), v in table.items() for d in (0, 1)}
    fallback = float(np.nanmedian(s.values)) if len(s) else 0.0
    _ = span_days
    return SeasonalProfile(tz, slot_minutes, table_mode or offset_mode, table, fallback)


def _slot_table(s: pd.Series, tz: str, slot_minutes: int, weekly: bool) -> dict[tuple[int, int], float]:
    keys = _slot_keys(s.index, tz, slot_minutes, weekly)
    grouped = pd.Series(s.values, index=keys).groupby(level=[0, 1]).median()
    # 칸 사이를 부드럽게(원형 이동 평균 3칸)
    table: dict[tuple[int, int], float] = {}
    n_slots = 1440 // slot_minutes
    for daytype in sorted(set(grouped.index.get_level_values(0))):
        part = grouped.xs(daytype, level=0).reindex(range(n_slots))
        part = part.interpolate(limit_direction="both")
        arr = part.to_numpy(dtype=float)
        smooth = (np.roll(arr, 1) + arr + np.roll(arr, -1)) / 3.0
        for slot, val in enumerate(smooth):
            if not np.isnan(val):
                table[(int(daytype), slot)] = float(val)
    return table


def group_runs(mask: np.ndarray, max_gap: int = 1) -> list[tuple[int, int]]:
    """True 구간 묶음 [(i0, i1)] — max_gap 이하 간격은 이어 붙인다."""
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return []
    runs = []
    start = prev = int(idx[0])
    for i in idx[1:]:
        i = int(i)
        if i - prev > max_gap:
            runs.append((start, prev))
            start = i
        prev = i
    runs.append((start, prev))
    return runs


def rolling_robust_sigma(values: np.ndarray, window: int) -> np.ndarray:
    s = pd.Series(values)
    med = s.rolling(window, center=True, min_periods=max(3, window // 2)).median()
    mad = (s - med).abs().rolling(window, center=True, min_periods=max(3, window // 2)).median()
    return (1.4826 * mad).to_numpy()


def theil_sen(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float, float]:
    """로버스트 기울기·절편과 기울기 95% 구간(scipy.stats.theilslopes)."""
    res = stats.theilslopes(y, x, 0.95)
    return float(res.slope), float(res.intercept), float(res.low_slope), float(res.high_slope)


def mae(actual: np.ndarray, pred: np.ndarray) -> float:
    a, p = np.asarray(actual, float), np.asarray(pred, float)
    ok = ~(np.isnan(a) | np.isnan(p))
    return float(np.mean(np.abs(a[ok] - p[ok]))) if ok.any() else float("nan")


def mape(actual: np.ndarray, pred: np.ndarray) -> float:
    """실제값이 0인 지점은 뺀다(ANA-07.02). 비율(0.05 = 5%)로 돌려준다."""
    a, p = np.asarray(actual, float), np.asarray(pred, float)
    ok = ~(np.isnan(a) | np.isnan(p)) & (a != 0)
    return float(np.mean(np.abs((a[ok] - p[ok]) / a[ok]))) if ok.any() else float("nan")


def psi(expected: np.ndarray, actual: np.ndarray, bins: int = 10) -> float:
    """PSI(분포 변화 지수, 학습 분포 10분위 기준, ANA-07.04)."""
    e = np.asarray(expected, float)
    a = np.asarray(actual, float)
    e, a = e[~np.isnan(e)], a[~np.isnan(a)]
    if e.size < bins or a.size == 0:
        return 0.0
    edges = np.unique(np.quantile(e, np.linspace(0, 1, bins + 1)))
    if edges.size < 2:
        return 0.0
    edges[0], edges[-1] = -np.inf, np.inf
    e_pct = np.histogram(e, edges)[0] / e.size
    a_pct = np.histogram(a, edges)[0] / a.size
    e_pct = np.clip(e_pct, 1e-4, None)
    a_pct = np.clip(a_pct, 1e-4, None)
    return float(np.sum((a_pct - e_pct) * np.log(a_pct / e_pct)))


def is_constant(values: np.ndarray, tol: float = 1e-9) -> bool:
    v = np.asarray(values, float)
    v = v[~np.isnan(v)]
    return v.size == 0 or float(np.nanmax(v) - np.nanmin(v)) <= tol


__all__ = ["robust_sigma"]


def decile_edges(values: np.ndarray, bins: int = 10) -> list[float]:
    v = np.asarray(values, float)
    v = v[~np.isnan(v)]
    if v.size == 0:
        return []
    return [float(x) for x in np.quantile(v, np.linspace(0, 1, bins + 1))]


def psi_from_edges(edges: list[float], actual: np.ndarray) -> float:
    """학습 때 저장한 10분위 경계로 PSI를 낸다(학습 분포는 칸마다 10%)."""
    a = np.asarray(actual, float)
    a = a[~np.isnan(a)]
    if len(edges) < 3 or a.size == 0:
        return 0.0
    inner = np.unique(np.asarray(edges[1:-1], float))
    bins = np.concatenate([[-np.inf], inner, [np.inf]])
    a_pct = np.histogram(a, bins)[0] / a.size
    e_pct = np.full(len(bins) - 1, 1.0 / (len(bins) - 1))
    a_pct = np.clip(a_pct, 1e-4, None)
    return float(np.sum((a_pct - e_pct) * np.log(a_pct / e_pct)))
