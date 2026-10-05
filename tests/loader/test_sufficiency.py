"""데이터 충분성 판정(ANA-03.03, ANA-08.01, BR-ANA-04)과 실행 한도(ANA-11.01, BR-ANA-07)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from data2flow_analytics.loader.bindings import Source
from data2flow_analytics.loader.loader import SeriesStats
from data2flow_analytics.loader.sufficiency import check, estimate_points, limit_violation
from data2flow_analytics.templates.base import Requirements

END = datetime(2026, 10, 5, tzinfo=UTC)
REQ = Requirements(min_period_days=7, min_points=100, missing_warn=0.10, missing_fail=0.20, recommended_resolution="RAW")


def _stats(days: float, received_ratio: float = 1.0, interval: float = 60.0, devices=1, quality=None, virtual=0) -> SeriesStats:
    expected = int(days * 86400 / interval)
    received = int(round(expected * received_ratio))
    st = SeriesStats(Source("target", "DEVICE_METRIC", "co2", 1), list(range(devices)), received=received, raw_total=received,
                     quality=quality or {0: received}, virtual_points=virtual, interval_sec=interval,
                     first=END - timedelta(days=days), last=END - timedelta(seconds=interval))
    return st


CASES = [
    # (기간 일, 수신 비율, 기대 수준, 메시지 일부)
    (3, 1.0, "FAIL", "최소 7일 데이터가 필요합니다(현재 3일)"),
    (7, 0.85, "WARN", "누락률 15.0%"),
    (7, 0.80, "WARN", "누락률 20.0%"),
    (7, 0.799, "FAIL", "누락률 20.1%"),
    (7, 0.95, "OK", None),
    (7, 0.90, "OK", None),
    (7, 0.899, "WARN", "경고 기준 10%"),
    (14, 1.0, "OK", None),
    (6.9, 1.0, "FAIL", "최소 7일"),
    (7, 0.0, "FAIL", "최소 100개 포인트"),
]


@pytest.mark.spec("ANA-03.03")
@pytest.mark.parametrize(("days", "ratio", "level", "text"), CASES)
def test_ANA_03_03_TC_ANA_086_sufficiency_table(days, ratio, level, text):
    """[ANA-03.03][AT-ANA-02.2·02.3] BR-ANA-04 판정표: 기간 부족 FAIL, 누락 15% WARN, 20.0% 경계 WARN, 20.1% FAIL, 포인트 부족 FAIL"""
    st = _stats(days, ratio)
    res = check([st], REQ, END - timedelta(days=days), END, "RAW", False)
    assert res.level == level
    if text:
        assert any(text in i["message"] for i in res.issues), res.issues
    s = res.stats
    assert set(s) >= {"points", "estimatedRawPoints", "missingRate", "qualityDistribution", "virtualPoints", "seriesCount", "effectiveResolution"}
    assert res.limits == {"maxPoints": 5_000_000, "maxSeries": 50, "timeoutSec": 600}


def test_quality_distribution_and_virtual_reporting():
    """품질 분포·가상 포인트 수 표시(가상 포함일 때만, AT-ANA-02.6)"""
    st = _stats(7, 1.0, quality={0: 9000, 1: 80, 3: 20}, virtual=500)
    res = check([st], REQ, END - timedelta(days=7), END, "RAW", True)
    assert res.stats["qualityDistribution"]["1"] == 80 and res.stats["virtualPoints"] == 500
    assert check([st], REQ, END - timedelta(days=7), END, "RAW", False).stats["virtualPoints"] == 0


def test_ANA_02_12_TC_ANA_074_cluster_min_30_days():
    """[ANA-02.12][TC-ANA-074] 하루 패턴 군집 25일 → 충분성 FAIL '최소 30일'"""
    from data2flow_analytics.templates.registry import build_registry

    req = build_registry().get("daily-profile-cluster").template.requirements
    res = check([_stats(25, interval=3600)], req, END - timedelta(days=25), END, "AUTO", False)
    assert res.level == "FAIL" and "최소 30일" in res.issues[0]["message"]


@pytest.mark.spec("ANA-11.01")
def test_ANA_11_01_TC_ANA_185_run_limits():
    """[ANA-11.01][AT-ANA-15.1] 시계열 51개 → 한도 초과, 예상 원본 800만 포인트 → suggest '1h', 1h면 13만 포인트로 한도 안"""
    many = [_stats(7) for _ in range(51)]
    res = check(many, REQ, END - timedelta(days=7), END, "RAW", False)
    assert limit_violation(res)["code"] == "TOO_MANY_SERIES"
    # 기기 16대(1분 주기, 공간 집계 하나) 347일 ≈ 800만 원본
    period = timedelta(days=347)
    big = _stats(347, devices=16)
    big.received = 8_000_000
    res = check([big], Requirements(7, 100, max_period_days=None), END - period, END, "RAW", False)
    violation = limit_violation(res)
    assert violation["code"] == "TOO_MANY_POINTS" and violation["fix"]["type"] == "SET_RESOLUTION"
    assert violation["fix"]["value"] in ("1m", "1h")
    assert estimate_points([big], "1h", period.total_seconds()) <= 130_000
    assert estimate_points([big], "1h", period.total_seconds()) < 5_000_000


def test_period_too_long_and_auto_resolution():
    req = Requirements(7, 100, recommended_resolution="1h", max_period_days=31)
    res = check([_stats(40, interval=3600)], req, END - timedelta(days=40), END, "AUTO", False)
    assert any(i["code"] == "PERIOD_TOO_LONG" for i in res.issues) and res.stats["effectiveResolution"] == "1h"
