"""에너지 롤업(ENE-01.03, BR-ENE-01·02·07). DB와 떨어진 순수 함수다.

- 공간 자기 값(BR-ENE-01): 측정 행이 있으면 측정, 없으면 추정, 둘 다 없으면 고지서 배분
- 상위 계량기(`coversDescendants`)가 그날 값을 가진 공간은 자기 값이 곧 합계이고 하위는 더하지 않는다(BR-ENE-02).
  상위 계량기가 없거나 그날 값이 없으면 자기 값 + 하위 합계
- 모든 노드에서 값 = 하위 합 + 기타(BR-ENE-07). 기타·공용 = 값 − 하위 합(상위 계량이 하위 합보다 작으면 음수로 드러낸다)
- 추정 kWh(추정 출처로 잡힌 몫)를 같이 굴려 추정 비율을 낸다
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from .compute import ESTIMATED, METERED

BILL_ALLOCATED = "BILL_ALLOCATED"
PRIORITY = (METERED, ESTIMATED, BILL_ALLOCATED)


@dataclass(frozen=True)
class Node:
    id: int
    parent_id: int | None = None
    name: str | None = None
    kind: str | None = None
    area_m2: float | None = None
    covers_descendants: bool = False


def _add(a: float | None, b: float | None) -> float | None:
    return b if a is None else a if b is None else a + b


def _sub(a: float | None, b: float | None) -> float | None:
    return None if a is None and b is None else (a or 0.0) - (b or 0.0)


@dataclass(frozen=True)
class Usage:
    kwh: float = 0.0
    krw: float | None = None
    tco2: float | None = None
    estimated_kwh: float = 0.0
    source: str | None = None  # 자기 값의 출처(롤업 값은 None)

    def __add__(self, o: Usage) -> Usage:
        return Usage(self.kwh + o.kwh, _add(self.krw, o.krw), _add(self.tco2, o.tco2), self.estimated_kwh + o.estimated_kwh)

    def __sub__(self, o: Usage) -> Usage:
        return Usage(self.kwh - o.kwh, _sub(self.krw, o.krw), _sub(self.tco2, o.tco2), self.estimated_kwh - o.estimated_kwh)

    @property
    def estimated_ratio(self) -> float | None:
        return None if self.kwh <= 0 else max(0.0, min(1.0, self.estimated_kwh / self.kwh))


ZERO = Usage()


def own_usage(rows: Iterable[Mapping]) -> dict[int, Usage]:
    """한 기간 칸의 `energy_daily` 행들 → 공간별 자기 값. 행은 space_id·source_kind·kwh·krw·tco2"""
    by: dict[int, dict[str, Usage]] = {}
    for r in rows:
        kind = r["source_kind"]
        kwh = float(r["kwh"])
        u = Usage(kwh, None if r.get("krw") is None else float(r["krw"]), None if r.get("tco2") is None else float(r["tco2"]),
                  kwh if kind == ESTIMATED else 0.0, kind)
        slot = by.setdefault(int(r["space_id"]), {})
        prev = slot.get(kind)
        slot[kind] = u if prev is None else Usage(prev.kwh + u.kwh, _add(prev.krw, u.krw), _add(prev.tco2, u.tco2),
                                                  prev.estimated_kwh + u.estimated_kwh, kind)
    out = {}
    for space, kinds in by.items():
        for kind in PRIORITY:
            if kind in kinds:
                out[space] = kinds[kind]
                break
    return out


@dataclass(frozen=True)
class Rolled:
    usage: Usage
    children: Usage  # 하위 합계
    covered: bool  # 상위 계량기 값으로 잡았다

    @property
    def other(self) -> Usage:
        return self.usage - self.children


def tree(nodes: Sequence[Node]) -> tuple[list[int], dict[int, list[int]]]:
    """루트들과 자식 목록. 부모가 목록에 없는 노드는 루트(권한 범위로 잘린 트리)"""
    ids = {n.id for n in nodes}
    kids: dict[int, list[int]] = {n.id: [] for n in nodes}
    roots = []
    for n in nodes:
        if n.parent_id is not None and n.parent_id in ids and n.parent_id != n.id:
            kids[n.parent_id].append(n.id)
        else:
            roots.append(n.id)
    return roots, kids


def rollup(nodes: Sequence[Node], own: Mapping[int, Usage]) -> dict[int, Rolled]:
    """노드마다 롤업 값. 순환이 있으면 이미 본 노드는 다시 더하지 않는다"""
    by_id = {n.id: n for n in nodes}
    roots, kids = tree(nodes)
    out: dict[int, Rolled] = {}
    seen: set[int] = set()
    for root in roots:
        stack = [(root, False)]
        while stack:
            nid, done = stack.pop()
            if done:
                child_sum = ZERO
                for c in kids[nid]:
                    if c in out:
                        child_sum = child_sum + out[c].usage
                mine = own.get(nid)
                if by_id[nid].covers_descendants and mine is not None:
                    out[nid] = Rolled(mine, child_sum, True)
                else:
                    out[nid] = Rolled(child_sum + (mine or ZERO), child_sum, False)
                continue
            if nid in seen:
                continue
            seen.add(nid)
            stack.append((nid, True))
            stack.extend((c, False) for c in kids[nid] if c not in seen)
    return out


def portfolio(nodes: Sequence[Node], rolled: Mapping[int, Rolled]) -> Rolled:
    """루트들의 합(포트폴리오). 포트폴리오에는 계량기가 없으니 기타는 0"""
    roots, _ = tree(nodes)
    total = ZERO
    for r in roots:
        total = total + rolled[r].usage
    return Rolled(total, total, False)


def area(nodes: Sequence[Node]) -> dict[int, float | None]:
    """노드 면적. 자기 면적이 있으면 그 값, 없으면 하위 면적 합(BR-ENE-06), 아무것도 없으면 None"""
    by_id = {n.id: n for n in nodes}
    roots, kids = tree(nodes)
    out: dict[int, float | None] = {}
    seen: set[int] = set()
    for root in roots:
        stack = [(root, False)]
        while stack:
            nid, done = stack.pop()
            if done:
                own = by_id[nid].area_m2
                if own is not None and own > 0:
                    out[nid] = float(own)
                else:
                    sub = [out[c] for c in kids[nid] if out.get(c) is not None]
                    out[nid] = sum(sub) if sub else None
                continue
            if nid in seen:
                continue
            seen.add(nid)
            stack.append((nid, True))
            stack.extend((c, False) for c in kids[nid] if c not in seen)
    return out
