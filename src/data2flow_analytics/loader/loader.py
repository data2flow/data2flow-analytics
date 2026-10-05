"""공통 로더(analytics-service.md §4.1). 바인딩을 SQL로 풀어 정렬된 DataFrame을 만든다. 템플릿 코드는 DB를 모른다.

읽기 전용 예외(conventions.md §6): `data2flow_pipeline`의 telemetry(+telemetry_long), telemetry_1m·1h·1d, device_state만 읽는다.
- NORMAL_ONLY + 집계 단위 1m·1h·1d → 연속 집계 테이블(avg는 품질 정상 값만, TSD)
- INCLUDE_ALL 또는 RAW → 원본에서 읽고(필요하면 date_bin으로 묶음)
- 가상 데이터는 include_virtual=true일 때만(BR-ANA-06)
- 공간 집계는 core-api API-DEV-128로 공간의 측정 기기를 풀어 같은 시각끼리 avg·max·min
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd
from psycopg_pool import ConnectionPool

from ..templates.base import InputData, SeriesInfo, Template
from .bindings import Source
from .core_client import CoreLookup

PIPE = "data2flow_pipeline"
RES_INTERVAL = {"1m": "1 minute", "1h": "1 hour", "1d": "1 day"}
RES_SECONDS = {"RAW": None, "1m": 60, "1h": 3600, "1d": 86400}
#: 1d 집계와 같은 기준(사이트 시간대 자정, BR-TSD-05). 기본 Asia/Seoul
DAY_ORIGIN = "2000-01-01 00:00:00+09"


def _raw_union(where: str) -> str:
    """원본 + 연장 보관 원본(TSD-02, M5)."""
    cols = "device_id, metric_key, time, value, quality, is_virtual"
    return (f"(SELECT {cols} FROM {PIPE}.telemetry WHERE {where} "
            f"UNION ALL SELECT {cols} FROM {PIPE}.telemetry_long WHERE {where})")


@dataclass
class SeriesStats:
    source: Source
    devices: list[int]
    received: int = 0  # 필터(품질·가상)를 통과한 원본 포인트
    raw_total: int = 0
    quality: dict[int, int] = field(default_factory=dict)
    virtual_points: int = 0
    interval_sec: float | None = None
    first: datetime | None = None
    last: datetime | None = None


@dataclass
class LoadRequest:
    organization_id: int
    sources: list[Source]
    period_from: datetime
    period_to: datetime
    resolution: str  # RAW | 1m | 1h | 1d (AUTO는 미리 정함)
    quality_filter: str = "NORMAL_ONLY"
    include_virtual: bool = False
    request_id: str | None = None


class TelemetryLoader:
    def __init__(self, pool: ConnectionPool, core: CoreLookup | None):
        self.pool = pool
        self.core = core

    # ---- 바인딩 → 기기 ----
    def devices_of(self, source: Source, request_id: str | None = None, cache: dict | None = None) -> list[int]:
        if source.device_id is not None and source.kind != "SPACE_AGGREGATE":
            return [source.device_id]
        cache = cache if cache is not None else {}
        if source.space_id not in cache:
            cache[source.space_id] = self.core.space_devices(source.space_id, request_id) if self.core else []
        return cache[source.space_id]

    # ---- 충분성 통계 ----
    def stats(self, req: LoadRequest) -> list[SeriesStats]:
        cache: dict = {}
        out = []
        with self.pool.connection() as conn:
            for src in req.sources:
                devices = self.devices_of(src, req.request_id, cache)
                st = SeriesStats(src, devices)
                if devices:
                    where = ("organization_id = %(org)s AND device_id = ANY(%(devices)s) AND metric_key = %(metric)s "
                             "AND time >= %(from)s AND time < %(to)s")
                    params = {"org": req.organization_id, "devices": devices, "metric": src.metric_key,
                              "from": req.period_from, "to": req.period_to}
                    rows = conn.execute(f"SELECT quality, is_virtual, count(*) AS n, min(time) AS first, max(time) AS last "
                                        f"FROM {_raw_union(where)} t GROUP BY quality, is_virtual", params).fetchall()
                    for r in rows:
                        st.raw_total += r["n"]
                        if r["is_virtual"] and not req.include_virtual:
                            continue
                        if r["is_virtual"]:
                            st.virtual_points += r["n"]
                        st.quality[int(r["quality"])] = st.quality.get(int(r["quality"]), 0) + r["n"]
                        if req.quality_filter == "INCLUDE_ALL" or int(r["quality"]) == 0:
                            st.received += r["n"]
                        st.first = r["first"] if st.first is None else min(st.first, r["first"])
                        st.last = r["last"] if st.last is None else max(st.last, r["last"])
                    interval = conn.execute(
                        "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY d) AS sec FROM ("
                        " SELECT extract(epoch FROM time - lag(time) OVER (PARTITION BY device_id ORDER BY time)) AS d"
                        f" FROM (SELECT device_id, time FROM {PIPE}.telemetry WHERE {where} ORDER BY time LIMIT 20000) s) x WHERE d > 0",
                        params).fetchone()
                    st.interval_sec = float(interval["sec"]) if interval and interval["sec"] else None
                out.append(st)
        return out

    # ---- 적재 ----
    def load(self, req: LoadRequest, template: Template) -> InputData:
        cache: dict = {}
        frames: dict[str, dict[str, pd.Series]] = {}
        qualities: dict[str, dict[str, pd.Series]] = {}
        infos: dict[str, SeriesInfo] = {}
        with self.pool.connection() as conn:
            for src in req.sources:
                devices = self.devices_of(src, req.request_id, cache)
                values, quality = self._series(conn, req, src, devices, template.needs_quality)
                frames.setdefault(src.role, {})[src.key] = values
                if quality is not None:
                    qualities.setdefault(src.role, {})[src.key] = quality
                infos[src.key] = SeriesInfo(key=src.key, role=src.role, label=src.display, kind=src.kind,
                                            device_id=src.device_id, space_id=src.space_id, metric_key=src.metric_key,
                                            semantic=src.inferred_semantic)
            extras = self._extras(conn, req, template, cache)
        out_frames = {role: _frame(cols, req.resolution) for role, cols in frames.items()}
        out_quality = {role: _frame(cols, req.resolution) for role, cols in qualities.items()}
        return InputData(frames=out_frames, series=infos, period_from=req.period_from, period_to=req.period_to,
                         resolution=None if req.resolution == "RAW" else req.resolution, quality=out_quality, extras=extras)

    def _series(self, conn, req: LoadRequest, src: Source, devices: list[int], needs_quality: bool):
        empty = pd.Series(dtype=float, index=pd.DatetimeIndex([], tz="UTC"))
        if not devices:
            return empty, (empty.copy() if needs_quality else None)
        params = {"org": req.organization_id, "devices": devices, "metric": src.metric_key, "from": req.period_from,
                  "to": req.period_to}
        virtual = "" if req.include_virtual else " AND NOT is_virtual"
        multi = len(devices) > 1
        agg_fn = {"avg": "avg", "max": "max", "min": "min"}[src.agg]
        if req.resolution != "RAW" and req.quality_filter == "NORMAL_ONLY" and not needs_quality:
            # 연속 집계 테이블은 워터마크까지만 확정이다(TSD). 그 뒤는 원본에서 같은 방식으로 묶는다
            mark = conn.execute(f"SELECT processed_until FROM {PIPE}.agg_watermarks WHERE level = %s", (req.resolution,)).fetchone()
            until = min(req.period_to, mark["processed_until"]) if mark else req.period_from
            rows: list = []
            if until > req.period_from:
                table = f"{PIPE}.telemetry_{req.resolution}"
                col = {"avg": "avg", "max": "max", "min": "min"}[src.agg]
                sql = (f"SELECT bucket AS t, {agg_fn}({col}) AS v FROM {table} WHERE organization_id = %(org)s "
                       f"AND device_id = ANY(%(devices)s) AND metric_key = %(metric)s AND bucket >= %(from)s AND bucket < %(until)s "
                       f"AND count > 0{virtual} GROUP BY bucket ORDER BY bucket")
                rows = conn.execute(sql, {**params, "until": until}).fetchall()
            if until < req.period_to:
                rows += self._binned(conn, req, src, {**params, "from": max(until, req.period_from)}, virtual, agg_fn, False)
            return _series(rows), None
        where = ("organization_id = %(org)s AND device_id = ANY(%(devices)s) AND metric_key = %(metric)s "
                 f"AND time >= %(from)s AND time < %(to)s{virtual}")
        if req.quality_filter == "NORMAL_ONLY" and not needs_quality:
            where += " AND quality = 0"
        if req.resolution == "RAW" and not multi:
            rows = conn.execute(f"SELECT time AS t, value AS v, quality AS q FROM {_raw_union(where)} r ORDER BY time", params).fetchall()
            return _series(rows), (_series(rows, "q") if needs_quality else None)
        rows = self._binned(conn, req, src, params, virtual, agg_fn, needs_quality)
        return _series(rows), (_series(rows, "q") if needs_quality else None)

    def _binned(self, conn, req: LoadRequest, src: Source, params: dict, virtual: str, agg_fn: str, needs_quality: bool) -> list:
        """원본을 집계 단위(date_bin)로 묶는다. 1d는 사이트 시간대 자정 기준."""
        where = ("organization_id = %(org)s AND device_id = ANY(%(devices)s) AND metric_key = %(metric)s "
                 f"AND time >= %(from)s AND time < %(to)s{virtual}")
        if req.quality_filter == "NORMAL_ONLY" and not needs_quality:
            where += " AND quality = 0"
        interval = RES_INTERVAL.get(req.resolution, "1 minute")
        origin = DAY_ORIGIN if req.resolution == "1d" else "2000-01-01 00:00:00+00"
        q_col = ", max(quality) AS q" if needs_quality else ""
        sql = (f"SELECT date_bin(interval '{interval}', time, timestamptz '{origin}') AS t, {agg_fn}(value) AS v{q_col} "
               f"FROM {_raw_union(where)} r GROUP BY 1 ORDER BY 1")
        return conn.execute(sql, params).fetchall()

    def _extras(self, conn, req: LoadRequest, template: Template, cache: dict) -> dict:
        extras: dict = {}
        if template.key in ("comfort-index",) and self.core:
            space_ids = [s.space_id for s in req.sources if s.space_id is not None]
            if space_ids:
                extras["spaceTargets"] = self.core.space_targets(space_ids[0], req.request_id)
        if template.key == "sensor-health":
            device_ids = sorted({d for s in req.sources for d in self.devices_of(s, req.request_id, cache)})
            if device_ids:
                rows = conn.execute(f"SELECT device_id, battery, rssi FROM {PIPE}.device_state WHERE organization_id = %(org)s "
                                    "AND device_id = ANY(%(ids)s)", {"org": req.organization_id, "ids": device_ids}).fetchall()
                extras["deviceStatus"] = {str(r["device_id"]): {"battery": _f(r["battery"]), "rssi": _f(r["rssi"])} for r in rows}
        return extras


def _f(v) -> float | None:
    return None if v is None else float(v)


def _series(rows: list[dict], col: str = "v") -> pd.Series:
    if not rows:
        return pd.Series(dtype=float, index=pd.DatetimeIndex([], tz="UTC"))
    idx = pd.DatetimeIndex([r["t"] for r in rows]).tz_convert("UTC")
    s = pd.Series([float(r[col]) if r[col] is not None else np.nan for r in rows], index=idx)
    return s[~s.index.duplicated(keep="first")].sort_index()


def _frame(cols: dict[str, pd.Series], resolution: str) -> pd.DataFrame:
    if not cols:
        return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC"))
    frame = pd.concat(cols, axis=1).sort_index()
    if resolution == "RAW" and frame.shape[1] > 1 and len(frame) > 2:
        # 기기마다 보고 시각이 달라 열이 어긋나면 공통 간격으로 맞춘다
        steps = [np.median(np.diff(s.dropna().index.values).astype("timedelta64[s]").astype(float))
                 for s in cols.values() if s.dropna().size > 2]
        if steps:
            step = pd.Timedelta(seconds=float(np.median(steps)))
            frame = frame.resample(step).mean()
    return frame
