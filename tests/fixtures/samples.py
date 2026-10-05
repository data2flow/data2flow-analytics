"""템플릿 13종의 고정 시드 표본 입력(계약 테스트·결정성·골든 결과에 함께 쓴다)."""

from __future__ import annotations

import pandas as pd

from . import synth
from .inputs import make_input

SEED = 7


def sample(key: str):
    """(InputData, params) — 템플릿별 대표 입력."""
    if key == "anomaly-detect":
        lab = synth.series(SEED, days=8, step="5min").spike(2, 600)
        return make_input({"target": {"co2": lab.series}}, units={"co2": "ppm"}), {"sensitivity": 3}
    if key == "forecast":
        return make_input({"target": {"co2": synth.forecastable(SEED, days=15)}}, units={"co2": "ppm"}), {"horizonHours": 24}
    if key == "threshold-eta":
        return make_input({"target": {"co2": synth.ramp(SEED, minutes=40)}}, units={"co2": "ppm"}), {"threshold": 1000}
    if key == "pattern-heatmap":
        return make_input({"target": {"noise": synth.occupancy_pattern(SEED, weeks=2, step="10min")}}, units={"noise": "dB"}), {}
    if key == "change-point":
        s, _ = synth.level_shift_series(SEED, 20, 0.6, days=40)
        return make_input({"target": {"pm25": s}}, units={"pm25": "µg/m³"}), {}
    if key == "correlation":
        df = synth.lagged(SEED, days=7)
        return make_input({"target": {"outdoor": df["outdoor"], "indoor": df["indoor"]}}), {"maxLagHours": 3}
    if key == "daily-profile-cluster":
        s, _ = synth.daily_profiles(SEED, days=35)
        return make_input({"target": {"kwh": s}}, units={"kwh": "kWh"}), {}
    if key == "comfort-index":
        df = synth.room(SEED, days=2)
        return make_input({"temp": {"temp": df["temp"]}, "humidity": {"humidity": df["humidity"]}, "co2": {"co2": df["co2"]}}), {}
    if key == "sensor-health":
        f = synth.fleet(SEED, devices=3, days=7)
        f = synth.stuck(f, 1, 6)
        return make_input({"devices": f}), {"peerCompare": True}
    if key == "battery-life":
        return make_input({"battery": {"b1": synth.battery(SEED, days=35), "b2": synth.battery(SEED + 1, days=35, slope=-0.5)}}), {}
    if key == "occupancy-estimate":
        df, _ = synth.occupancy(SEED, days=7)
        return make_input({"co2": {"co2": df["co2"]}, "activity": {"act": df["activity"]}}), {}
    if key == "intervention-impact":
        df, at = synth.intervention(SEED, effect=-0.2, days_before=8, days_after=8)
        return make_input({"target": {"pm25": df["target"]}, "covariates": {"out": df["outdoor"]}}), {"interventionAt": at.isoformat(),
                                                                                                         "bootstrap": 300}
    if key == "space-benchmark":
        a, b = synth.room(SEED, days=7), synth.room(SEED + 1, days=7)
        idx = a.index
        data = make_input({"temp": {"t1": a["temp"], "t2": b["temp"]}, "humidity": {"h1": a["humidity"], "h2": b["humidity"]},
                           "co2": {"c1": a["co2"], "c2": b["co2"]}, "energy": {"e1": pd.Series(1.0, index=idx), "e2": pd.Series(2.0, index=idx)}})
        for k, space in (("t1", 1), ("h1", 1), ("c1", 1), ("e1", 1), ("t2", 2), ("h2", 2), ("c2", 2), ("e2", 2)):
            info = data.series[k]
            data.series[k] = type(info)(**{**info.__dict__, "space_id": space})
        return data, {"spaces": [{"spaceId": "1", "name": "강의실 A", "areaM2": 50}, {"spaceId": "2", "name": "강의실 B"}]}
    raise KeyError(key)


def provenance(data, seed: int = SEED) -> dict:
    """실행기가 붙이는 공통 출처(계약 테스트에서 결과 스키마를 통과시키기 위함)."""
    return {"template": "x@1.0.0", "bindings": [], "period": {"from": data.period_from.isoformat(), "to": data.period_to.isoformat()},
            "resolution": "RAW", "points": data.total_points(), "missingRate": 0.0, "qualityFilter": "NORMAL_ONLY", "virtual": False,
            "seed": seed}
