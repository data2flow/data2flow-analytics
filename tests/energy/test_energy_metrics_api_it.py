"""에너지 지표 내부 API(API-ENE-85 ← core API-ENE-10·11): energy_daily 롤업, 일·월 칸, 비교, 하위 단위(Testcontainers PostgreSQL 18)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from tests.conftest import HEADERS, ORG

SITE, BLDG, R1, R2 = 1, 10, 101, 102
SPACES = [{"id": SITE, "parentId": None, "name": "캠퍼스", "kind": "SITE"},
          {"id": BLDG, "parentId": SITE, "name": "본관", "kind": "BUILDING", "coversDescendants": True},
          {"id": R1, "parentId": BLDG, "name": "101", "kind": "ROOM", "areaM2": 40},
          {"id": R2, "parentId": BLDG, "name": "102", "kind": "ROOM", "areaM2": 60}]


def _put(deps, space, day, kind, kwh, krw=None, tco2=None, org=ORG):
    with deps.pool.connection() as conn:
        conn.execute("""INSERT INTO data2flow_analytics.energy_daily (organization_id, space_id, day, source_kind, kwh, krw, tco2,
                        includes_estimate) VALUES (%s, %s, %s, %s, %s, %s, %s, false)""", (org, space, day, kind, kwh, krw, tco2))


@pytest.fixture
def seeded(deps):
    for i in range(3):
        d = date(2026, 9, 29) + timedelta(days=i)  # 9/29, 9/30, 10/1
        _put(deps, R1, d, "METERED", 100, 15000, 0.0456)
        _put(deps, R2, d, "ESTIMATED", 200, 30000, 0.0912)
        _put(deps, R2, d, "METERED", 150, 22500, 0.0684)  # 측정이 추정을 이긴다
    _put(deps, BLDG, date(2026, 9, 30), "METERED", 300, 45000, 0.137)  # 상위 계량기는 하루만
    _put(deps, R1, date(2026, 9, 28), "METERED", 50, 7500, 0.02)  # 직전 기간
    _put(deps, R1, date(2026, 9, 30), "METERED", 999, org=ORG + 1)  # 다른 조직
    return deps


def _post(client, **body):
    return client.post("/internal/analytics/energy/metrics", json={"spaces": SPACES, **body}, headers=HEADERS)


def test_building_metrics_by_day(client, seeded):
    res = _post(client, scopeSpaceId=BLDG, **{"from": "2026-09-29", "to": "2026-10-01"}, granularity="DAY", compare="PREVIOUS_PERIOD")
    assert res.status_code == 200, res.text
    body = res.json()["response"]
    assert body["scope"] == {"spaceId": BLDG, "name": "본관", "kind": "BUILDING", "areaM2": 100.0}
    t = body["totals"]
    assert t["kwh"] == 250 + 300 + 250  # 9/30은 상위 계량기 300
    assert t["otherCommonKwh"] == 50
    assert t["estimatedRatio"] == 0
    assert t["euiKwhPerM2"] == 8.0
    assert t["kwhPerOccupiedHour"] is None and t["occupiedHoursReason"] == "NO_OCCUPANCY"
    assert [s["t"] for s in body["series"]] == ["2026-09-29", "2026-09-30", "2026-10-01"]
    assert [s["kwh"] for s in body["series"]] == [250, 300, 250]
    assert body["compare"]["kind"] == "PREVIOUS_PERIOD" and body["compare"]["kwh"] == 50
    assert body["compare"]["from"] == "2026-09-26" and body["compare"]["to"] == "2026-09-28"
    kids = {c["id"]: c for c in body["children"]}
    assert kids[R1]["kwh"] == 300 and kids[R1]["euiKwhPerM2"] == 7.5 and kids[R1]["diffRatio"] == 5.0
    assert kids[R2]["kwh"] == 450 and kids[R2]["estimated"] is False
    assert body["other"]["kwh"] == 50
    assert sum(c["kwh"] for c in body["children"]) + body["other"]["kwh"] == t["kwh"]


def test_portfolio_by_month(client, seeded):
    body = _post(client, **{"from": "2026-09-01", "to": "2026-10-31"}, granularity="MONTH").json()["response"]
    assert body["scope"]["spaceId"] is None
    assert [(s["t"], s["kwh"]) for s in body["series"]] == [("2026-09", 50 + 250 + 300), ("2026-10", 250)]
    assert [c["id"] for c in body["children"]] == [SITE]
    assert body["compare"] is None


def test_estimated_ratio_when_only_estimates(client, deps):
    _put(deps, R2, date(2026, 9, 29), "ESTIMATED", 200)
    body = _post(client, scopeSpaceId=SITE, **{"from": "2026-09-29", "to": "2026-09-29"}, compare="BASELINE").json()["response"]
    assert body["totals"]["estimatedRatio"] == 1.0
    assert body["compare"]["reason"] == "NO_BASELINE"


def test_validation(client, seeded):
    assert _post(client, **{"from": "2026-09-29", "to": "2026-09-28"}).status_code == 400
    assert _post(client, **{"from": "2024-01-01", "to": "2026-01-01"}).status_code == 400
    assert _post(client, **{"from": "2026-09-29", "to": "2026-09-30"}, granularity="HOUR").status_code == 400
    assert _post(client, **{"from": "2026-09-29", "to": "2026-09-30"}, compare="X").status_code == 400
    assert _post(client, scopeSpaceId=999, **{"from": "2026-09-29", "to": "2026-09-30"}).status_code == 404
    assert client.post("/internal/analytics/energy/metrics", json={"spaces": [{"x": 1}], "from": "2026-09-29", "to": "2026-09-29"},
                       headers=HEADERS).status_code == 400
    assert _post(client, **{"to": "2026-09-30"}).status_code == 400
