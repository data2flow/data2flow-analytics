"""에너지 일 집계(ENE-01.01·01.02, `energy_daily`, TC-ENE-006).

- 매일 조직 시간대 02:00이 지나면 전날을 확정한다. 원천의 날짜 경계는 원천 사이트 시간대 자정이다
- 측정 원천: 원본 텔레메트리(품질 정상)를 누적이면 차분, 순간 전력이면 사다리꼴 적분하고 1시간 칸으로 나눠 그 시각의 단가로 비용을 낸다(BR-ENE-03·05)
- 추정 원천(BR-ENE-04): 정격 × 그날 가동 시간(action 가동 집계 ACT-08.02) + 대기 × 나머지. 가동 기록이 없으면 대기 전력만, action에 닿지 않으면
  그날 추정 행을 쓰지 않는다. 시간대별 단가는 하루에 고르게 나눠 적용한다
- 공간 × 출처마다 한 행(kWh·원·tCO2·추정 포함 여부)
- 서버가 여러 대여도 조직별 advisory lock 안에서 `energy_daily_runs`를 먼저 넣은 인스턴스만 쓰고 EVT-ENE-01을 한 번 낸다
- 설정 묶음(API-ENE-70)은 5분 캐시한다
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..deps import Deps
from ..events import publisher as ev
from ..loader.action_client import ActionClient, RuntimeLookup
from ..loader.loader import _raw_union
from .compute import (
    ESTIMATED,
    METERED,
    EmissionFactor,
    Tariff,
    Tariffs,
    carbon,
    clip_hourly,
    cost,
    diff_cumulative,
    estimated_kwh,
    integrate_power,
)

log = logging.getLogger("data2flow_analytics.energy")

S = "data2flow_analytics"
LOCK = 0x0E7E01  # 조직별 에너지 일 집계 advisory lock 네임스페이스
DUE_HOUR = 2
CACHE_TTL = timedelta(minutes=5)
LOOKBACK = timedelta(days=1)  # 날짜 경계 앞뒤 표본을 찾는 범위(첫 구간·끝 구간)


@dataclass
class _Acc:
    site_id: int | None
    timezone: str
    kwh: float = 0.0
    estimated: bool = False
    hourly: list = field(default_factory=list)


class EnergyDaily:
    def __init__(self, deps: Deps, action: RuntimeLookup | None = None):
        self.deps = deps
        self.action = action if action is not None else ActionClient(deps.settings.action_uri)
        self._configs: dict[int, tuple[datetime, dict]] = {}
        self._orgs: tuple[datetime | None, list[int]] = (None, [])

    @property
    def core(self):
        core = self.deps.loader.core
        return core if core is not None and hasattr(core, "energy_config") else None

    # ---- 설정 ----
    def config(self, org: int) -> dict:
        now = self.deps.clock.now()
        hit = self._configs.get(org)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
        cfg = self.core.energy_config(org)
        self._configs[org] = (now, cfg)
        return cfg

    def invalidate(self, org: int | None = None) -> None:
        """EVT-ENE-07을 받으면 그 조직 캐시를 지운다"""
        if org is None:
            self._configs.clear()
        else:
            self._configs.pop(int(org), None)

    def organizations(self) -> list[int]:
        now = self.deps.clock.now()
        at, orgs = self._orgs
        if at is None or now - at >= CACHE_TTL:
            orgs = self.core.energy_organizations()
            self._orgs = (now, orgs)
        return orgs

    # ---- 매일 02:00 ----
    def tick(self) -> list[dict]:
        """확정할 때가 된 조직의 전날을 쓴다. 이미 쓴 날은 건너뛴다"""
        if self.core is None:
            return []
        out = []
        now = self.deps.clock.now()
        for org in self.organizations():
            try:
                cfg = self.config(org)
                local = now.astimezone(ZoneInfo(cfg.get("timezone") or "UTC"))
                if local.hour < DUE_HOUR:
                    continue
                day = local.date() - timedelta(days=1)
                if self._done(org, day):
                    continue
                res = self.compute(org, day, cfg=cfg)
                if res is not None:
                    out.append(res)
            except Exception:  # noqa: BLE001 — 한 조직이 실패해도 다른 조직은 계속
                log.exception("energy daily failed org=%s", org)
        return out

    def _done(self, org: int, day: date) -> bool:
        with self.deps.pool.connection() as conn:
            return conn.execute(f"SELECT 1 FROM {S}.energy_daily_runs WHERE organization_id = %s AND day = %s", (org, day)).fetchone() is not None

    # ---- 한 조직·하루 ----
    def compute(self, org: int, day: date, *, cfg: dict | None = None, recomputed: bool = False) -> dict | None:
        """그날의 공간 × 출처 행을 쓰고 EVT-ENE-01을 낸다. 다른 인스턴스가 이미 썼거나 쓰는 중이면 None(recomputed면 다시 쓴다)"""
        cfg = cfg if cfg is not None else self.config(org)
        rows = self._rows(org, day, cfg)
        now = self.deps.clock.now()
        with self.deps.pool.connection() as conn:
            if not conn.execute("SELECT pg_try_advisory_xact_lock(%s, %s) AS ok", (LOCK, int(org) % 2_147_483_647)).fetchone()["ok"]:
                return None
            if recomputed:
                conn.execute(f"""INSERT INTO {S}.energy_daily_runs (organization_id, day, space_count, computed_at) VALUES (%s, %s, %s, %s)
                                 ON CONFLICT (organization_id, day) DO UPDATE SET space_count = EXCLUDED.space_count,
                                 computed_at = EXCLUDED.computed_at""", (org, day, len({r[0] for r in rows}), now))
            elif conn.execute(f"""INSERT INTO {S}.energy_daily_runs (organization_id, day, space_count, computed_at) VALUES (%s, %s, %s, %s)
                                  ON CONFLICT DO NOTHING RETURNING day""", (org, day, len({r[0] for r in rows}), now)).fetchone() is None:
                return None
            conn.execute(f"DELETE FROM {S}.energy_daily WHERE organization_id = %s AND day = %s AND source_kind IN ('METERED', 'ESTIMATED')",
                         (org, day))
            for space_id, kind, kwh, krw, tco2, estimated in rows:
                conn.execute(f"""INSERT INTO {S}.energy_daily (organization_id, space_id, day, source_kind, kwh, krw, tco2, includes_estimate,
                                 computed_at, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                             (org, space_id, day, kind, kwh, krw, tco2, estimated, now, now, now))
        space_ids = sorted({r[0] for r in rows})
        payload = {"days": [day.isoformat()], "spaceIds": [str(s) for s in space_ids], "recomputed": recomputed}
        ev.safe_publish(self.deps.publisher, ev.make_event(self.deps.clock, ev.ENERGY_DAILY, org, payload))
        return {"organizationId": org, "day": day.isoformat(), "spaces": len(space_ids), "rows": len(rows)}

    def _rows(self, org: int, day: date, cfg: dict) -> list[tuple]:
        """(공간, 출처, kWh, 원, tCO2, 추정 포함) 행들"""
        tariff_rows = [Tariff.of(t) for t in cfg.get("tariffs") or []]
        holidays = {int(site): frozenset(date.fromisoformat(d) for d in days) for site, days in (cfg.get("holidays") or {}).items()}
        factors = [EmissionFactor(date.fromisoformat(f["validFrom"]), float(f["factorTco2PerMwh"])) for f in cfg.get("emissionFactors") or []]
        acc: dict[tuple[int, str], _Acc] = {}
        sources = cfg.get("meterSources") or []
        running = self._running(org, day, [int(s["deviceId"]) for s in sources if s.get("kind") == ESTIMATED])
        for src in sources:
            site = int(src["siteId"]) if src.get("siteId") else None
            tz = src.get("timezone") or cfg.get("timezone") or "UTC"
            start = datetime.combine(day, time(0), ZoneInfo(tz)).astimezone(ZoneInfo("UTC"))
            end = datetime.combine(day + timedelta(days=1), time(0), ZoneInfo(tz)).astimezone(ZoneInfo("UTC"))
            if src.get("kind") == ESTIMATED:
                if running is None or not src.get("ratedPowerW"):
                    continue
                span = (end - start).total_seconds()
                kwh = estimated_kwh(float(src["ratedPowerW"]), src.get("standbyPowerW"), running.get(int(src["deviceId"]), 0), span)
                a = acc.setdefault((int(src["spaceId"]), ESTIMATED), _Acc(site, tz))
                a.kwh += kwh
                a.estimated = True
                hours = max(1, round(span / 3600))
                a.hourly.extend((start + timedelta(hours=h), kwh / hours) for h in range(hours))
                continue
            if src.get("kind") != METERED:
                continue
            samples = self._samples(int(src["deviceId"]), src["metricKey"], start, end)
            cumulative = src.get("cumulative")
            if cumulative is None:
                cumulative = src["metricKey"] == "energy"
            hourly = clip_hourly(diff_cumulative(samples) if cumulative else integrate_power(samples), start, end)
            a = acc.setdefault((int(src["spaceId"]), METERED), _Acc(site, tz))
            for h in hourly:
                a.kwh += h.kwh
                a.estimated |= h.estimated
                a.hourly.append((h.start, h.kwh))
        out = []
        for (space_id, kind), a in sorted(acc.items()):
            priced = cost(a.hourly, Tariffs(tariff_rows, holidays.get(a.site_id, frozenset())), a.timezone, site_id=a.site_id)
            krw = None if priced.krw is None else round(priced.krw, 2)
            out.append((space_id, kind, a.kwh, krw, carbon([(day, a.kwh)], factors), a.estimated))
        return out

    def _running(self, org: int, day: date, device_ids: list[int]) -> dict[int, int] | None:
        """추정 원천 기기의 그날 가동 초. 기기가 없으면 빈 값, action에 닿지 않으면 None"""
        if not device_ids:
            return {}
        try:
            return self.action.on_seconds(org, day, sorted(set(device_ids)))
        except Exception:  # noqa: BLE001 — 가동 시간을 모르면 추정 행을 쓰지 않는다(측정 행은 쓴다)
            log.warning("runtime unavailable org=%s day=%s", org, day, exc_info=True)
            return None

    def _samples(self, device_id: int, metric: str, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
        """[start, end) 안의 품질 정상 원본과 경계 앞뒤 한 점(하루 안)"""
        where = "device_id = %(d)s AND metric_key = %(m)s AND quality = 0 AND value IS NOT NULL"
        sql = (f"(SELECT time, value FROM {_raw_union(where + ' AND time >= %(lo)s AND time < %(s)s')} t ORDER BY time DESC LIMIT 1) "
               f"UNION ALL (SELECT time, value FROM {_raw_union(where + ' AND time >= %(s)s AND time < %(e)s')} t) "
               f"UNION ALL (SELECT time, value FROM {_raw_union(where + ' AND time >= %(e)s AND time < %(hi)s')} t ORDER BY time LIMIT 1) "
               "ORDER BY time")
        params = {"d": device_id, "m": metric, "s": start, "e": end, "lo": start - LOOKBACK, "hi": end + LOOKBACK}
        with self.deps.pool.connection() as conn:
            return [(r["time"], float(r["value"])) for r in conn.execute(sql, params).fetchall()]

