"""ANA-09.02 공간 비교."""

from __future__ import annotations

import pandas as pd
import pytest

from data2flow_analytics.templates.builtin.space_benchmark.template import TEMPLATE as T
from tests.fixtures import synth
from tests.fixtures.inputs import ctx, make_input


@pytest.mark.spec("ANA-09.02")
def test_ANA_09_02_TC_ANA_170_missing_area_excluded():
    """[ANA-09.02][AT-ANA-13.1] 강의실 3곳 중 1곳 면적 없음 → ㎡당 에너지 null + '면적 없음', 그 공간은 순위에서 빠지고 2곳만 순위"""
    roles: dict = {"temp": {}, "humidity": {}, "co2": {}, "energy": {}}
    spaces = []
    for i, sid in enumerate(("1", "2", "3")):
        room = synth.room(10 + i, days=7)
        roles["temp"][f"t{sid}"] = room["temp"] + i
        roles["humidity"][f"h{sid}"] = room["humidity"]
        roles["co2"][f"c{sid}"] = room["co2"]
        roles["energy"][f"e{sid}"] = pd.Series(1.0 + i, index=room.index)
        spaces.append({"spaceId": sid, "name": f"강의실 {sid}", "areaM2": None if sid == "3" else 50.0 * (i + 1), "purpose": "강의"})
    data = make_input(roles)
    for key, info in list(data.series.items()):
        data.series[key] = type(info)(**{**info.__dict__, "space_id": int(key[1:])})
    res = T.run(data, T.parse_params({"spaces": spaces}), ctx(1))
    rows = {r["spaceId"]: r for r in res.tables[0]["rows"]}
    assert rows["3"]["energyPerM2"] is None and "면적 없음" in rows["3"]["reason"] and rows["3"]["energyPerM2Rank"] is None
    assert sorted(r["energyPerM2Rank"] for k, r in rows.items() if k != "3") == [1, 2]
    assert rows["1"]["comfortScoreRank"] == 1
