"""템플릿 단위 테스트용 InputData 만들기(로더 없이)."""

from __future__ import annotations

import pandas as pd

from data2flow_analytics.templates.base import InputData, RunContext, SeriesInfo

NOW = pd.Timestamp("2026-10-05T00:00:00Z").to_pydatetime()


def make_input(roles: dict[str, dict[str, pd.Series]], *, units: dict[str, str] | None = None, resolution: str | None = None,
               extras: dict | None = None, quality: dict | None = None, semantics: dict[str, str] | None = None) -> InputData:
    frames, infos = {}, {}
    starts, ends = [], []
    for role, cols in roles.items():
        frame = pd.concat({k: v for k, v in cols.items()}, axis=1).sort_index() if cols else pd.DataFrame()
        frames[role] = frame
        for k, s in cols.items():
            infos[k] = SeriesInfo(key=k, role=role, label=k, metric_key=k, unit=(units or {}).get(k),
                                  semantic=(semantics or {}).get(k), device_id=abs(hash(k)) % 1000 + 1)
            if len(s):
                starts.append(s.index[0])
                ends.append(s.index[-1])
    step = None
    for f in frames.values():
        if len(f.index) > 1:
            step = f.index[1] - f.index[0]
            break
    end = max(ends) + (step or pd.Timedelta(0)) if ends else pd.Timestamp(NOW)
    return InputData(frames=frames, series=infos, period_from=min(starts).to_pydatetime() if starts else NOW,
                     period_to=end.to_pydatetime(), resolution=resolution, extras=extras or {}, quality=quality or {})


def ctx(seed: int = 42, **kw) -> RunContext:
    return RunContext(seed=seed, now=NOW, **kw)
