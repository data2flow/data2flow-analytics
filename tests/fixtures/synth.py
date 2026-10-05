"""합성 데이터 생성기(ANA test-plan "합성 데이터와 정확도 기준").

모든 함수는 seed를 받아 numpy.random.default_rng(seed)만 쓴다. 기본 형태는 기준값 + 일주기(sin, 24h) + 주주기(평일·주말 차) +
가우스 노이즈이고, 그 위에 spike·drop·level_shift·variance·stuck·drop_rate(누락)·offset(드리프트)·combo_anomaly(다변량)를 주입한다.
주입 위치와 정답 라벨을 함께 돌려준다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

START = pd.Timestamp("2026-08-03T00:00:00Z")  # 월요일 09:00 KST 직전(UTC 자정)


def _index(days: float, step: str, start: pd.Timestamp = START) -> pd.DatetimeIndex:
    periods = int(round(pd.Timedelta(days=days) / pd.Timedelta(step)))
    return pd.date_range(start, periods=periods, freq=step, tz="UTC")


def _daily(idx: pd.DatetimeIndex, tz: str = "Asia/Seoul") -> np.ndarray:
    local = idx.tz_convert(tz)
    hours = local.hour + local.minute / 60.0
    return np.sin(2 * np.pi * (hours - 9) / 24.0)  # 15시 근처 최고


def _weekend(idx: pd.DatetimeIndex, tz: str = "Asia/Seoul") -> np.ndarray:
    return (idx.tz_convert(tz).dayofweek >= 5).astype(float)


@dataclass
class Labeled:
    series: pd.Series
    labels: list[dict] = field(default_factory=list)
    rng: np.random.Generator | None = None
    sigma: float = 1.0

    def _free_positions(self, n: int, margin: int) -> list[int]:
        size = len(self.series)
        taken = [(lab["i0"] - margin, lab["i1"] + margin) for lab in self.labels]
        out: list[int] = []
        tries = 0
        while len(out) < n and tries < 10_000:
            tries += 1
            pos = int(self.rng.integers(margin * 2, size - margin * 2))
            if any(a <= pos <= b for a, b in taken):
                continue
            out.append(pos)
            taken.append((pos - margin, pos + margin))
        return sorted(out)

    def spike(self, n: int = 3, delta: float = 800.0) -> Labeled:
        s = self.series.copy()
        for pos in self._free_positions(n, margin=120):
            s.iloc[pos] += delta
            self.labels.append({"kind": "SPIKE" if delta > 0 else "DROP", "i0": pos, "i1": pos,
                                "start": s.index[pos], "end": s.index[pos]})
        self.series = s
        return self

    def drop(self, n: int = 1, delta: float = -500.0) -> Labeled:
        return self.spike(n=n, delta=delta)

    def level_shift(self, hours: float = 6, delta: float = 200.0, n: int = 1) -> Labeled:
        s = self.series.copy()
        step = s.index[1] - s.index[0]
        width = int(pd.Timedelta(hours=hours) / step)
        for pos in self._free_positions(n, margin=width + 60):
            s.iloc[pos:pos + width] += delta
            self.labels.append({"kind": "LEVEL_SHIFT", "i0": pos, "i1": pos + width - 1,
                                "start": s.index[pos], "end": s.index[pos + width - 1]})
        self.series = s
        return self

    def variance(self, hours: float = 6, factor: float = 3.0) -> Labeled:
        s = self.series.copy()
        step = s.index[1] - s.index[0]
        width = int(pd.Timedelta(hours=hours) / step)
        pos = self._free_positions(1, margin=width + 60)[0]
        extra = self.rng.normal(0, self.sigma * np.sqrt(factor ** 2 - 1), width)
        s.iloc[pos:pos + width] += extra
        self.labels.append({"kind": "VARIANCE", "i0": pos, "i1": pos + width - 1,
                            "start": s.index[pos], "end": s.index[pos + width - 1]})
        self.series = s
        return self


def series(seed: int, days: float = 14, step: str = "1min", base: float = 600.0, daily_amp: float = 150.0,
           weekend_delta: float = -60.0, noise: float = 20.0) -> Labeled:
    rng = np.random.default_rng(seed)
    idx = _index(days, step)
    values = base + daily_amp * _daily(idx) + weekend_delta * _weekend(idx) + rng.normal(0, noise, len(idx))
    return Labeled(pd.Series(values, index=idx, name="target"), rng=rng, sigma=noise)


def mixed_anomalies(seed: int, days: float = 30, step: str = "10min", n: int = 20) -> Labeled:
    """야간 정확도용: 급등·급락·수준 이동 혼합 n개."""
    lab = series(seed, days=days, step=step)
    kinds = np.random.default_rng(seed + 1000).choice(["SPIKE", "DROP", "LEVEL_SHIFT"], size=n)
    for k in kinds:
        if k == "SPIKE":
            lab.spike(1, delta=float(lab.rng.uniform(300, 800)))
        elif k == "DROP":
            lab.spike(1, delta=-float(lab.rng.uniform(300, 600)))
        else:
            lab.level_shift(hours=float(lab.rng.uniform(3, 8)), delta=float(lab.rng.choice([-1, 1]) * lab.rng.uniform(150, 300)))
    return lab


def seasonal(seed: int, days: float = 30, step: str = "1min", daily: float = 3.0, weekly: float = 1.5,
             base: float = 600.0, sigma: float = 20.0) -> pd.Series:
    """진폭을 노이즈 σ 배수로 주는 계절성 시계열(이상 없음)."""
    rng = np.random.default_rng(seed)
    idx = _index(days, step)
    values = base + daily * sigma * _daily(idx) - weekly * sigma * _weekend(idx) + rng.normal(0, sigma, len(idx))
    return pd.Series(values, index=idx, name="target")


def forecastable(seed: int, days: float = 21, step: str = "1h", base: float = 700.0, noise_ratio: float = 0.05,
                 pattern: int = 0) -> pd.Series:
    """예측용 CO2: 일주기 + 주주기 + 노이즈(값의 noise_ratio). pattern 0~2는 계절 패턴 3종(야간 정확도)."""
    rng = np.random.default_rng(seed)
    idx = _index(days, step)
    local = idx.tz_convert("Asia/Seoul")
    hours = local.hour + local.minute / 60.0
    weekend = local.dayofweek >= 5
    if pattern == 0:
        shape = 1 + 0.4 * np.sin(2 * np.pi * (hours - 9) / 24.0)
    elif pattern == 1:
        shape = 1 + 0.6 * np.exp(-((hours - 14) ** 2) / 8.0)
    else:
        shape = 1 + 0.3 * np.sin(2 * np.pi * (hours - 6) / 24.0) + 0.15 * np.sin(4 * np.pi * hours / 24.0)
    level = base * shape * np.where(weekend, 0.8, 1.0)
    values = level * (1 + rng.normal(0, noise_ratio, len(idx)))
    return pd.Series(values, index=idx, name="co2")


def multivariate(seed: int, days: float = 21, step: str = "10min") -> tuple[pd.DataFrame, dict | None]:
    """온도·습도·CO2: 재실 요인 f(t)에 함께 오르는 정상 관계."""
    rng = np.random.default_rng(seed)
    idx = _index(days, step)
    f = np.clip(_daily(idx), 0, None) * (1 - 0.6 * _weekend(idx))
    df = pd.DataFrame({
        "temp": 22 + 3 * f + rng.normal(0, 0.25, len(idx)),
        "humidity": 42 + 12 * f + rng.normal(0, 1.0, len(idx)),
        "co2": 500 + 600 * f + rng.normal(0, 30, len(idx)),
    }, index=idx)
    return df, None


def combo_anomaly(df: pd.DataFrame, day: int = 15) -> tuple[pd.DataFrame, dict]:
    """하루 동안 '고온·저습·고CO2' 조합: 각 값은 정상 범위 안이지만 습도만 관계를 거스른다."""
    out = df.copy()
    start = df.index[0] + pd.Timedelta(days=day) + pd.Timedelta(hours=1)  # 10시 KST부터
    end = start + pd.Timedelta(hours=8)
    mask = (out.index >= start) & (out.index < end)
    out.loc[mask, "temp"] = 24.6 + (out.loc[mask, "temp"] - out.loc[mask, "temp"].mean()) * 0.3
    out.loc[mask, "co2"] = 1050 + (out.loc[mask, "co2"] - out.loc[mask, "co2"].mean()) * 0.3
    out.loc[mask, "humidity"] = 43.0 + (out.loc[mask, "humidity"] - out.loc[mask, "humidity"].mean()) * 0.3
    return out, {"start": start, "end": end, "injected": ["humidity", "temp", "co2"], "driver": "humidity"}


def fleet(seed: int, devices: int = 5, days: float = 7, step: str = "10min") -> dict[str, pd.Series]:
    rng = np.random.default_rng(seed)
    idx = _index(days, step)
    base = 23 + 2 * _daily(idx)
    return {f"dev{i}": pd.Series(base + rng.normal(0, 0.2, len(idx)), index=idx) for i in range(devices)}


def stuck(fleet_: dict[str, pd.Series], dev: int, hours: float, at_day: float = 2.0) -> dict[str, pd.Series]:
    s = fleet_[f"dev{dev}"].copy()
    start = s.index[0] + pd.Timedelta(days=at_day)
    mask = (s.index >= start) & (s.index < start + pd.Timedelta(hours=hours))
    s[mask] = round(float(s[mask].iloc[0]), 1)
    fleet_[f"dev{dev}"] = s
    return fleet_


def drop_rate(fleet_: dict[str, pd.Series], dev: int, rate: float, seed: int = 0) -> dict[str, pd.Series]:
    rng = np.random.default_rng(seed)
    s = fleet_[f"dev{dev}"].copy()
    keep = rng.random(len(s)) >= rate
    fleet_[f"dev{dev}"] = s[keep]
    return fleet_


def offset(fleet_: dict[str, pd.Series], dev: int, delta: float) -> dict[str, pd.Series]:
    fleet_[f"dev{dev}"] = fleet_[f"dev{dev}"] + delta
    return fleet_


def battery(seed: int, days: float = 60, slope: float = -0.2, start: float = 80.0, noise: float = 0.3,
            step: str = "1h") -> pd.Series:
    rng = np.random.default_rng(seed)
    idx = _index(days, step)
    t = (idx - idx[0]) / pd.Timedelta(days=1)
    return pd.Series(start + slope * np.asarray(t) + rng.normal(0, noise, len(idx)), index=idx)


def occupancy_pattern(seed: int, weeks: int = 5, step: str = "1min", tz: str = "Asia/Seoul") -> pd.Series:
    """소음 LAeq: 평일 9~18시 +15dB."""
    rng = np.random.default_rng(seed)
    idx = _index(weeks * 7, step)
    local = idx.tz_convert(tz)
    busy = (local.dayofweek < 5) & (local.hour >= 9) & (local.hour < 18)
    return pd.Series(38 + 15 * busy + rng.normal(0, 2.0, len(idx)), index=idx)


def room(seed: int, days: float = 7, step: str = "10min") -> pd.DataFrame:
    """쾌적한 기본 실내(22~24℃, 45~55%, 600~800ppm)."""
    rng = np.random.default_rng(seed)
    idx = _index(days, step)
    d = _daily(idx)
    return pd.DataFrame({
        "temp": 23 + 0.8 * d + rng.normal(0, 0.1, len(idx)),
        "humidity": 50 + 3 * d + rng.normal(0, 0.5, len(idx)),
        "co2": 700 + 80 * d + rng.normal(0, 10, len(idx)),
    }, index=idx)


def ramp(seed: int, start: float = 850.0, slope: float = 15.0, minutes: int = 30, noise: float = 3.0,
         at: pd.Timestamp = START) -> pd.Series:
    rng = np.random.default_rng(seed)
    idx = pd.date_range(at, periods=minutes + 1, freq="1min", tz="UTC")
    t = np.arange(len(idx), dtype=float) - minutes  # 마지막 점이 start
    return pd.Series(start + slope * t + rng.normal(0, noise, len(idx)), index=idx)


def occupancy(seed: int, days: float = 14, step: str = "1min") -> tuple[pd.DataFrame, pd.Series]:
    """CO2 + activity(적외선 카운트)와 재실 정답 라벨."""
    rng = np.random.default_rng(seed)
    idx = _index(days, step)
    local = idx.tz_convert("Asia/Seoul")
    occ = np.zeros(len(idx), dtype=int)
    day_starts = pd.date_range(local[0].normalize(), local[-1].normalize(), freq="1D")
    for day in day_starts:
        if day.dayofweek >= 5:
            continue
        for _ in range(int(rng.integers(2, 4))):
            begin = day + pd.Timedelta(hours=float(rng.uniform(9, 16)))
            dur = pd.Timedelta(minutes=float(rng.uniform(50, 120)))
            occ[(local >= begin) & (local < begin + dur)] = 1
    activity = np.where(occ == 1, rng.poisson(2.5, len(idx)), (rng.random(len(idx)) < 0.003).astype(int))
    co2 = np.empty(len(idx))
    level = 450.0
    for i in range(len(idx)):
        target = 1100.0 if occ[i] else 450.0
        rate = 0.03 if occ[i] else 0.02
        level += (target - level) * rate
        co2[i] = level + rng.normal(0, 8)
    df = pd.DataFrame({"co2": co2, "activity": activity.astype(float)}, index=idx)
    return df, pd.Series(occ, index=idx)


def lagged(seed: int, lag: str = "2h", r: float = 0.8, days: float = 30, step: str = "10min") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = _index(days, step)
    lag_steps = int(pd.Timedelta(lag) / pd.Timedelta(step))
    n = len(idx) + lag_steps
    # 외기 온도: 부드러운 AR(1) + 일주기
    ar = np.zeros(n)
    for i in range(1, n):
        ar[i] = 0.9 * ar[i - 1] + rng.normal(0, 1)
    outdoor_full = (ar - ar.mean()) / ar.std()
    outdoor = outdoor_full[lag_steps:]
    shifted = outdoor_full[:len(idx)]  # t-lag 시점의 외기
    indoor = r * shifted + np.sqrt(1 - r ** 2) * rng.normal(0, 1, len(idx))
    return pd.DataFrame({"outdoor": 15 + 5 * outdoor, "indoor": 22 + 2 * indoor}, index=idx)


def daily_profiles(seed: int, days: int = 60, holidays: int = 6) -> tuple[pd.Series, list[int]]:
    """전력량(1h): 평일·주말·휴일 3종 프로필 + 노이즈. 정답 라벨(0 평일, 1 주말, 2 휴일)을 날짜 순으로."""
    rng = np.random.default_rng(seed)
    hours = np.arange(24)
    weekday = 2 + 8 * ((hours >= 9) & (hours < 18))
    weekend = 2 + 1.0 * np.sin(2 * np.pi * hours / 24) ** 2
    holiday = 2 + 4 * np.exp(-((hours - 12) ** 2) / 4)
    local_days = pd.date_range(START.tz_convert("Asia/Seoul").normalize(), periods=days, freq="1D")
    weekday_idx = [i for i, d in enumerate(local_days) if d.dayofweek < 5]
    holiday_set = set(rng.choice(weekday_idx, size=holidays, replace=False).tolist())
    values, labels = [], []
    for i, d in enumerate(local_days):
        if i in holiday_set:
            prof, lab = holiday, 2
        elif d.dayofweek >= 5:
            prof, lab = weekend, 1
        else:
            prof, lab = weekday, 0
        values.extend(prof + rng.normal(0, 0.4, 24))
        labels.append(lab)
    idx = pd.date_range(local_days[0], periods=days * 24, freq="1h").tz_convert("UTC")
    return pd.Series(values, index=idx), labels


def level_shift_series(seed: int, at_days: list[int] | int = 30, ratio: float = 0.6, days: int = 60, base: float = 35.0,
                       step: str = "1h", noise_ratio: float = 0.15) -> tuple[pd.Series, list[pd.Timestamp]]:
    """PM2.5: 변화 시점마다 평균이 ratio배."""
    rng = np.random.default_rng(seed)
    idx = _index(days, step)
    points = [at_days] if isinstance(at_days, int) else list(at_days)
    level = np.full(len(idx), base)
    changes = []
    for i, day in enumerate(points):
        t = idx[0] + pd.Timedelta(days=day)
        factor = ratio if i % 2 == 0 else 1 / ratio
        level = np.where(idx >= t, level * factor, level)
        changes.append(t)
    values = level * (1 + 0.25 * _daily(idx) - 0.1 * _weekend(idx)) * (1 + rng.normal(0, noise_ratio, len(idx)))
    return pd.Series(values, index=idx), changes


def intervention(seed: int, effect: float = -0.2, days_before: int = 14, days_after: int = 14, noise: float = 0.1,
                 base: float = 30.0) -> tuple[pd.DataFrame, pd.Timestamp]:
    """개입 전후(PM2.5) + 외기 온도. 개입 뒤 값이 (1 + effect)배."""
    rng = np.random.default_rng(seed)
    idx = _index(days_before + days_after, "1h")
    at = idx[0] + pd.Timedelta(days=days_before)
    outdoor = 18 + 6 * _daily(idx) + rng.normal(0, 1.0, len(idx))
    level = base * (1 + 0.3 * _daily(idx) - 0.15 * _weekend(idx)) + 0.3 * (outdoor - 18)
    level = np.where(idx >= at, level * (1 + effect), level)
    values = level * (1 + rng.normal(0, noise, len(idx)))
    return pd.DataFrame({"target": values, "outdoor": outdoor}, index=idx), at


def shifted(seed: int, delta: float = 6.0, n: int = 2000) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    return rng.normal(22, 1.0, n), rng.normal(22 + delta, 1.0, n)
