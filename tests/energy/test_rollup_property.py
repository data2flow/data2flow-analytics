"""롤업 속성(TC-ENE-018): 무작위 트리(깊이 5, 노드 ≤ 50)의 모든 노드에서 값 = 하위 합 + 기타, 포트폴리오 = 루트 합."""

from __future__ import annotations

import pytest
from hypothesis import given, seed, settings
from hypothesis import strategies as st

from data2flow_analytics.energy.rollup import Node, own_usage, portfolio, rollup, tree

KINDS = ("SITE", "BUILDING", "FLOOR", "ROOM", "ZONE")


@st.composite
def forests(draw):
    n = draw(st.integers(1, 50))
    nodes, depth = [], {}
    for i in range(1, n + 1):
        parents = [p for p in depth if depth[p] < 4]
        parent = draw(st.one_of(st.none(), st.sampled_from(parents))) if parents else None
        depth[i] = 0 if parent is None else depth[parent] + 1
        nodes.append(Node(i, parent, kind=KINDS[depth[i]], covers_descendants=draw(st.booleans())))
    rows = []
    for node in nodes:
        for kind in draw(st.lists(st.sampled_from(("METERED", "ESTIMATED", "BILL_ALLOCATED")), max_size=3, unique=True)):
            rows.append({"space_id": node.id, "source_kind": kind, "kwh": draw(st.floats(0, 1e5, allow_nan=False)),
                         "krw": draw(st.one_of(st.none(), st.floats(0, 1e7, allow_nan=False))), "tco2": None})
    return nodes, rows


@seed(20261029)
@settings(max_examples=300, deadline=None)
@given(forests())
def test_tc_ene_018_value_equals_children_plus_other(forest):
    nodes, rows = forest
    own = own_usage(rows)
    rolled = rollup(nodes, own)
    roots, kids = tree(nodes)
    assert set(rolled) == {n.id for n in nodes}
    for n in nodes:
        r = rolled[n.id]
        children = sum(rolled[c].usage.kwh for c in kids[n.id])
        assert r.usage.kwh == pytest.approx(children + r.other.kwh, rel=1e-9, abs=1e-6)
        if n.covers_descendants and n.id in own:
            assert r.usage.kwh == own[n.id].kwh  # 상위 계량기 값 그대로(이중 계산 없음)
        else:
            assert r.other.kwh == pytest.approx(own[n.id].kwh if n.id in own else 0.0, abs=1e-6)
        assert 0 <= r.usage.estimated_kwh <= r.usage.kwh + 1e-6
    assert portfolio(nodes, rolled).usage.kwh == pytest.approx(sum(rolled[r].usage.kwh for r in roots), rel=1e-9, abs=1e-6)
