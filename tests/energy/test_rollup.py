"""롤업(ENE-01.03, TC-ENE-017): 하위 합, 상위 계량기의 기타·공용, 이중 계산 방지, 추정 비율."""

from __future__ import annotations

import pytest

from data2flow_analytics.energy.rollup import Node, Usage, area, own_usage, portfolio, rollup

SITE, BLDG, R1, R2, R3 = 1, 10, 101, 102, 103


def _nodes(covers: bool = False) -> list[Node]:
    return [Node(SITE, None, "캠퍼스", "SITE"), Node(BLDG, SITE, "본관", "BUILDING", covers_descendants=covers),
            Node(R1, BLDG, "101", "ROOM", 30.0), Node(R2, BLDG, "102", "ROOM", 50.0), Node(R3, BLDG, "103", "ROOM")]


def _row(space, kind, kwh, krw=None, tco2=None):
    return {"space_id": space, "source_kind": kind, "kwh": kwh, "krw": krw, "tco2": tco2}


ROOMS = [_row(R1, "METERED", 100, 15000, 0.04), _row(R2, "METERED", 200, 30000, 0.08), _row(R3, "ESTIMATED", 300, 45000, 0.12)]


def test_tc_ene_017_building_is_sum_of_rooms():
    rolled = rollup(_nodes(), own_usage(ROOMS))
    b = rolled[BLDG]
    assert b.usage.kwh == 600
    assert b.usage.krw == 90000
    assert b.usage.tco2 == pytest.approx(0.24)
    assert b.other.kwh == 0
    assert b.usage.estimated_ratio == pytest.approx(0.5)
    assert rolled[SITE].usage.kwh == 600


def test_tc_ene_017_covering_meter_wins_and_shows_other_common():
    rows = ROOMS + [_row(BLDG, "METERED", 650, 97500, 0.26)]
    rolled = rollup(_nodes(covers=True), own_usage(rows))
    b = rolled[BLDG]
    assert b.covered
    assert b.usage.kwh == 650  # 하위를 이중으로 더하지 않는다
    assert b.children.kwh == 600
    assert b.other.kwh == 50
    assert b.usage.estimated_ratio == 0  # 건물 값은 측정
    assert rolled[SITE].usage.kwh == 650
    assert portfolio(_nodes(covers=True), rolled).usage.kwh == 650


def test_covering_meter_without_data_falls_back_to_children():
    rolled = rollup(_nodes(covers=True), own_usage(ROOMS))
    assert rolled[BLDG].usage.kwh == 600
    assert not rolled[BLDG].covered


def test_non_covering_own_meter_is_added_as_other():
    rolled = rollup(_nodes(), own_usage(ROOMS + [_row(BLDG, "METERED", 40)]))
    assert rolled[BLDG].usage.kwh == 640
    assert rolled[BLDG].other.kwh == 40


def test_metered_beats_estimated_in_same_space():
    own = own_usage([_row(R1, "ESTIMATED", 90), _row(R1, "METERED", 100), _row(R2, "BILL_ALLOCATED", 7), _row(R3, "ESTIMATED", 3),
                     _row(R3, "BILL_ALLOCATED", 9)])
    assert (own[R1].kwh, own[R1].source, own[R1].estimated_kwh) == (100, "METERED", 0)
    assert own[R2].source == "BILL_ALLOCATED"
    assert (own[R3].kwh, own[R3].source) == (3, "ESTIMATED")


def test_unknown_cost_stays_none():
    rolled = rollup(_nodes(), own_usage([_row(R1, "METERED", 10)]))
    assert rolled[BLDG].usage.krw is None
    assert (Usage(1, 5.0) + Usage(2)).krw == 5.0


def test_area_uses_own_then_children():
    nodes = _nodes()
    a = area(nodes)
    assert a[R3] is None and a[BLDG] == 80.0
    nodes[1] = Node(BLDG, SITE, area_m2=500.0)
    assert area(nodes)[BLDG] == 500.0


def test_cut_tree_and_cycles():
    """권한으로 부모가 빠진 노드는 루트, 순환은 한 번만 센다"""
    cut = [Node(R1, BLDG), Node(R2, BLDG)]
    rolled = rollup(cut, own_usage(ROOMS))
    assert portfolio(cut, rolled).usage.kwh == 300
    cyc = [Node(1, None), Node(2, 3), Node(3, 2)]
    rolled = rollup(cyc, own_usage([_row(2, "METERED", 5), _row(3, "METERED", 7)]))
    assert rolled[1].usage.kwh == 0 and set(rolled) == {1}
