"""에너지 지표·하위 단위(API-ENE-10·11의 계산, 내부 API API-ENE-85). core-api가 권한 범위로 자른 공간 트리를 넘긴다.

- `energy_daily`를 날마다 롤업(rollup.py)해 범위 노드의 값·기타·추정 비율을 내고, 일/월 칸으로 묶는다
- 비교: 직전 같은 길이 기간(PREVIOUS_PERIOD), 작년 같은 기간(SAME_PERIOD_LAST_YEAR). 기준선(BASELINE)은 ENE-01.04 뒤에 채운다
- HOUR 칸은 `telemetry_1h`에서 계산하는 별도 경로라 아직 받지 않는다
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from ..common.errors import BusinessError, ErrorCode, FieldError
from ..deps import Deps
from .compute import eui, per_occupied_hour, round_carbon
from .rollup import ZERO, Node, Rolled, Usage, area, own_usage, portfolio, rollup, tree

S = "data2flow_analytics"
MAX_DAYS = 730
GRANULARITIES = ("DAY", "MONTH")
COMPARES = ("PREVIOUS_PERIOD", "SAME_PERIOD_LAST_YEAR", "BASELINE")


def _bad(field: str, message: str) -> BusinessError:
    return BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError(field, "Invalid", message)])


def _date(body: dict, key: str) -> date:
    try:
        return date.fromisoformat(str(body[key])[:10])
    except (KeyError, ValueError) as exc:
        raise _bad(key, "YYYY-MM-DD 날짜가 필요합니다") from exc


def _nodes(body: dict) -> list[Node]:
    try:
        return [Node(int(s["id"]), None if s.get("parentId") is None else int(s["parentId"]), s.get("name"), s.get("kind"),
                     None if s.get("areaM2") is None else float(s["areaM2"]), bool(s.get("coversDescendants")))
                for s in body.get("spaces") or []]
    except (KeyError, ValueError, TypeError) as exc:
        raise _bad("spaces", "공간은 id·parentId·areaM2·coversDescendants로 줍니다") from exc


def _shift_year(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 2월 29일
        return d.replace(year=d.year + years, day=28)


def _r(v: float | None, n: int) -> float | None:
    return None if v is None else round(v, n)


class EnergyMetrics:
    def __init__(self, deps: Deps):
        self.deps = deps

    def metrics(self, org: int, body: dict) -> dict:
        start, end = _date(body, "from"), _date(body, "to")
        if end < start or (end - start).days + 1 > MAX_DAYS:
            raise _bad("to", f"기간은 1~{MAX_DAYS}일입니다")
        granularity = body.get("granularity") or "DAY"
        if granularity not in GRANULARITIES:
            raise _bad("granularity", "DAY·MONTH만 됩니다(HOUR는 아직 지원하지 않습니다)")
        compare = body.get("compare")
        if compare is not None and compare not in COMPARES:
            raise _bad("compare", ", ".join(COMPARES))
        nodes = _nodes(body)
        by_id = {n.id: n for n in nodes}
        scope = body.get("scopeSpaceId")
        scope = None if scope in (None, "") else int(scope)
        if scope is not None and scope not in by_id:
            raise BusinessError(ErrorCode.RESOURCE_NOT_FOUND)

        days = self._daily(org, [n.id for n in nodes], start, end)
        roots, kids = tree(nodes)
        child_ids = kids[scope] if scope is not None else roots
        areas = area(nodes)

        total, children, other = ZERO, defaultdict(lambda: ZERO), ZERO
        series: dict[str, Usage] = defaultdict(lambda: ZERO)
        spaces_with_data: set[int] = set()
        for day, rows in sorted(days.items()):
            rolled = rollup(nodes, own_usage(rows))
            node = rolled[scope] if scope is not None else portfolio(nodes, rolled)
            total = total + node.usage
            other = other + node.other
            for c in child_ids:
                children[c] = children[c] + rolled[c].usage
            key = day.isoformat() if granularity == "DAY" else day.strftime("%Y-%m")
            series[key] = series[key] + node.usage
            spaces_with_data |= {int(r["space_id"]) for r in rows}

        scope_area = areas.get(scope) if scope is not None else _sum([areas.get(r) for r in roots])
        intensity = eui(total.kwh, scope_area)
        per_hour = per_occupied_hour(total.kwh, None, None)  # 재실 시간(occupancy_est)은 OCC·ENE-02.02 뒤에 채운다
        compared = self._compare(org, nodes, scope, start, end, compare, total) if compare else None
        prev_children = compared.pop("_children", {}) if compared else {}
        return {
            "scope": {"spaceId": scope, "name": by_id[scope].name if scope is not None else None, "kind": by_id[scope].kind if scope is not None else None,
                      "areaM2": scope_area},
            "totals": {"kwh": _r(total.kwh, 3), "krw": _r(total.krw, 2), "tco2": round_carbon(total.tco2),
                       "euiKwhPerM2": _r(intensity.value, 4), "euiReason": intensity.reason,
                       "kwhPerOccupiedHour": _r(per_hour.value, 4), "occupiedHoursSource": per_hour.denominator_source,
                       "occupiedHoursReason": per_hour.reason,
                       "estimatedRatio": _r(total.estimated_ratio, 4), "otherCommonKwh": _r(other.kwh, 3)},
            "compare": compared,
            "series": [{"t": t, "kwh": _r(u.kwh, 3), "krw": _r(u.krw, 2), "tco2": round_carbon(u.tco2), "estimatedKwh": _r(u.estimated_kwh, 3)}
                       for t, u in sorted(series.items())],
            "children": [self._child(by_id[c], children[c], areas.get(c), total, prev_children.get(c), c in spaces_with_data or children[c].kwh > 0)
                         for c in child_ids],
            "other": {"kwh": _r(other.kwh, 3)},
        }

    @staticmethod
    def _child(node: Node, u: Usage, area_m2: float | None, total: Usage, prev: float | None, has_data: bool) -> dict:
        diff = None if prev is None or prev <= 0 else (u.kwh - prev) / prev
        return {"id": node.id, "name": node.name, "kind": node.kind, "kwh": _r(u.kwh, 3), "krw": _r(u.krw, 2), "tco2": round_carbon(u.tco2),
                "euiKwhPerM2": _r(eui(u.kwh, area_m2).value, 4), "estimatedRatio": _r(u.estimated_ratio, 4), "estimated": u.estimated_kwh > 0,
                "share": None if total.kwh <= 0 else round(u.kwh / total.kwh, 4), "diffRatio": _r(diff, 4), "hasData": has_data}

    def _compare(self, org: int, nodes: list[Node], scope: int | None, start: date, end: date, kind: str, total: Usage) -> dict:
        if kind == "BASELINE":
            return {"kind": kind, "kwh": None, "diffRatio": None, "reason": "NO_BASELINE"}
        if kind == "PREVIOUS_PERIOD":
            length = (end - start).days + 1
            p_start, p_end = start - timedelta(days=length), start - timedelta(days=1)
        else:
            p_start, p_end = _shift_year(start, -1), _shift_year(end, -1)
        roots, kids = tree(nodes)
        child_ids = kids[scope] if scope is not None else roots
        prev, prev_children = ZERO, defaultdict(float)
        for _, rows in self._daily(org, [n.id for n in nodes], p_start, p_end).items():
            rolled: dict[int, Rolled] = rollup(nodes, own_usage(rows))
            prev = prev + (rolled[scope] if scope is not None else portfolio(nodes, rolled)).usage
            for c in child_ids:
                prev_children[c] += rolled[c].usage.kwh
        diff = None if prev.kwh <= 0 else (total.kwh - prev.kwh) / prev.kwh
        return {"kind": kind, "from": p_start.isoformat(), "to": p_end.isoformat(), "kwh": _r(prev.kwh, 3), "krw": _r(prev.krw, 2),
                "diffRatio": _r(diff, 4), "_children": dict(prev_children)}

    def _daily(self, org: int, space_ids: list[int], start: date, end: date) -> dict[date, list[dict]]:
        if not space_ids:
            return {}
        with self.deps.pool.connection() as conn:
            rows = conn.execute(f"""SELECT space_id, day, source_kind, kwh, krw, tco2, occupied_hours FROM {S}.energy_daily
                                    WHERE organization_id = %s AND space_id = ANY(%s) AND day BETWEEN %s AND %s""",
                                (org, space_ids, start, end)).fetchall()
        out: dict[date, list[dict]] = defaultdict(list)
        for r in rows:
            out[r["day"]].append(r)
        return out


def _sum(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) if vals else None
