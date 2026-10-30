"""환기율(ACH) 추정(ENE-02.01, BR-ENE-09). DB와 떨어진 순수 함수다.

- 감쇠 구간: CO2가 줄어드는 연속 구간. 잡음에 흔들리지 않게 3점 이동 중앙값으로 보고, 구간 최솟값보다 허용폭(중앙값 잡음의 5배, 최소 10ppm)
  넘게 오르면 최솟값 자리에서 끊는다. C − C_out이 바닥(50ppm) 아래로 내려가면 끝낸다(꼬리는 ln이 잡음에 휘둘린다)
- 조건: 30분 이상, 시작값 − C_out ≥ 200ppm
- 적합: ln(C − C_out) 대 시간(시) 최소제곱. ACH = −기울기, 95% 신뢰 구간 = 기울기 ± t·표준오차. R² < 0.8이면 버린다
- 환기량(㎥/h) = ACH × 부피(면적 × 층고). 부피가 없으면 계산하지 않는다
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import numpy as np
from scipy import stats

OUTDOOR_PPM = 420.0
MIN_MINUTES = 30
MIN_AMPLITUDE = 200.0
MIN_R2 = 0.8
FLOOR_PPM = 50.0
MIN_TOLERANCE = 10.0
SMOOTH = 3

# 추정하지 않은 사유
LOW_AMPLITUDE = "LOW_AMPLITUDE"
TOO_SHORT = "TOO_SHORT"
LOW_FIT = "LOW_FIT"
NO_DATA = "NO_DATA"


@dataclass(frozen=True)
class Estimate:
    ach: float
    ci_low: float
    ci_high: float
    r2: float
    decay_from: datetime
    decay_to: datetime
    points: int


@dataclass(frozen=True)
class Result:
    """추정값 또는 추정하지 않은 사유"""

    estimate: Estimate | None
    reason: str | None = None


def _smooth(values: np.ndarray) -> np.ndarray:
    half = SMOOTH // 2
    return np.array([float(np.median(values[max(0, i - half):i + half + 1])) for i in range(len(values))])


def _noise(values: np.ndarray) -> float:
    """1차 차분의 MAD로 잡음 표준편차를 어림한다"""
    if len(values) < 3:
        return 0.0
    d = np.diff(values)
    mad = float(np.median(np.abs(d - np.median(d))))
    return mad / 0.6745 / np.sqrt(2)


def segments(times: Sequence[datetime], values: Sequence[float], outdoor: float = OUTDOOR_PPM) -> list[tuple[int, int]]:
    """감쇠 구간 [시작, 끝] 인덱스(끝 포함). 길이·진폭 조건은 보지 않는다"""
    v = np.asarray(values, dtype=float)
    if len(v) < 2:
        return []
    s = _smooth(v)
    tolerance = max(MIN_TOLERANCE, 5 * _noise(v) * 1.2 / np.sqrt(SMOOTH))
    out: list[tuple[int, int]] = []
    start, low, at = None, 0.0, 0
    for i in range(len(s)):
        above = s[i] - outdoor >= FLOOR_PPM
        if start is None:
            if above and i + 1 < len(s) and s[i + 1] <= s[i]:
                start, low, at = i, s[i], i
            continue
        rose = s[i] - low > tolerance
        if not above or rose:
            end = at if rose else i - 1
            if end > start:
                out.append((start, end))
            start, low, at = (i, s[i], i) if above else (None, 0.0, 0)
            continue
        if s[i] <= low:
            low, at = s[i], i
    if start is not None and at > start:
        out.append((start, at if len(s) - 1 - at > SMOOTH else len(s) - 1))
    return out


def fit(times: Sequence[datetime], values: Sequence[float], outdoor: float = OUTDOOR_PPM) -> Result:
    """한 감쇠 구간의 ACH. 조건에 맞지 않으면 사유"""
    v = np.asarray(values, dtype=float)
    if len(v) < 3:
        return Result(None, TOO_SHORT)
    minutes = (times[-1] - times[0]).total_seconds() / 60
    if minutes < MIN_MINUTES:
        return Result(None, TOO_SHORT)
    if float(np.median(v[:SMOOTH])) - outdoor < MIN_AMPLITUDE:
        return Result(None, LOW_AMPLITUDE)
    keep = v - outdoor > 0
    if keep.sum() < 3:
        return Result(None, LOW_FIT)
    t = np.array([(x - times[0]).total_seconds() / 3600 for x in times])[keep]
    y = np.log(v[keep] - outdoor)
    reg = stats.linregress(t, y)
    r2 = float(reg.rvalue ** 2)
    if reg.slope >= 0 or r2 < MIN_R2:
        return Result(None, LOW_FIT)
    q = float(stats.t.ppf(0.975, len(t) - 2))
    ach = -float(reg.slope)
    half = q * float(reg.stderr)
    return Result(Estimate(round(ach, 4), round(ach - half, 4), round(ach + half, 4), round(r2, 4), times[0], times[-1], int(len(t))))


def estimate(times: Sequence[datetime], values: Sequence[float], outdoor: float = OUTDOOR_PPM) -> Result:
    """시계열에서 가장 잘 맞는 감쇠 구간의 ACH. 없으면 가장 앞선 단계의 사유(진폭 → 길이 → 적합)"""
    if len(values) < 3:
        return Result(None, NO_DATA)
    best: Estimate | None = None
    reasons: list[str] = []
    for a, b in segments(times, values, outdoor) or [(0, len(values) - 1)]:
        r = fit(times[a:b + 1], values[a:b + 1], outdoor)
        if r.estimate is None:
            reasons.append(r.reason or LOW_FIT)
        elif best is None or r.estimate.r2 > best.r2:
            best = r.estimate
    if best is not None:
        return Result(best)
    for reason in (LOW_FIT, TOO_SHORT, LOW_AMPLITUDE):
        if reason in reasons:
            return Result(None, reason)
    return Result(None, NO_DATA)


def volume_m3(area_m2: float | None, ceiling_height_m: float | None) -> float | None:
    if not area_m2 or not ceiling_height_m or area_m2 <= 0 or ceiling_height_m <= 0:
        return None
    return area_m2 * ceiling_height_m


def ventilation_m3h(ach: float, volume: float | None) -> float | None:
    return None if volume is None else round(ach * volume, 2)


__all__ = ["Estimate", "Result", "estimate", "fit", "segments", "volume_m3", "ventilation_m3h"]
