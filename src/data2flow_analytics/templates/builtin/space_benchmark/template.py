"""space-benchmark 공간 비교(ANA-09.02). 같은 용도의 공간끼리 쾌적도, 활용률, ㎡당 에너지를 순위로 비교한다.

공간 면적·용도(DEV-01.02)가 없으면 해당 지표를 빼고 이유를 표시한다. 공간 정보는 파라미터 spaces로 받는다(core-api가 채움).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from data2flow_analytics.templates.base import (
    Category,
    InputData,
    Kind,
    Metric,
    Requirements,
    Result,
    RoleSpec,
    RunContext,
    SeriesInfo,
    Template,
    TemplateParams,
    chart,
    table,
)
from data2flow_analytics.templates.builtin.comfort_index.template import TEMPLATE as COMFORT
from data2flow_analytics.templates.builtin.occupancy_estimate.template import TEMPLATE as OCCUPANCY


class SpaceMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spaceId: str = Field(description="공간 ID")
    name: str = Field("", description="표시 이름")
    areaM2: float | None = Field(None, gt=0, description="면적(㎡). 없으면 ㎡당 에너지를 계산하지 않는다")
    purpose: str | None = Field(None, description="용도(같은 용도끼리 순위를 매긴다)")


class Params(TemplateParams):
    spaces: list[SpaceMeta] = Field([], description="비교할 공간의 이름·면적·용도. 바인딩의 공간 ID와 맞춘다")


class SpaceBenchmark(Template):
    key = "space-benchmark"
    version = "1.0.0"
    name = "공간 비교"
    kind = Kind.DOMAIN
    category = Category.SPACE_USAGE
    roles = [RoleSpec("temp", "multi_series", 0, 50, "temperature", required=False, label="온도(공간별)"),
             RoleSpec("humidity", "multi_series", 0, 50, "humidity", required=False, label="습도(공간별)"),
             RoleSpec("co2", "multi_series", 0, 50, "co2", required=False, label="CO2(공간별)"),
             RoleSpec("activity", "multi_series", 0, 50, "activity", required=False, label="적외선 활동(공간별)"),
             RoleSpec("energy", "multi_series", 0, 50, "energy", required=False, label="전력량 kWh(공간별)")]
    params_model = Params
    requirements = Requirements(min_period_days=7, min_points=100, missing_warn=0.10, missing_fail=0.30,
                                recommended_resolution="1h", max_period_days=366)
    fast = True
    outputs = ["bar", "table"]
    fixed_caveats = ["공간 비교는 용도·면적·재실이 비슷하다고 가정합니다. 용도가 다르면 순위를 그대로 비교하지 마세요."]

    def run(self, data: InputData, params: Params, ctx: RunContext) -> Result:
        meta = {m.spaceId: m for m in params.spaces}
        by_space: dict[str, dict[str, str]] = {}
        for key, info in data.series.items():
            if info.space_id is None:
                continue
            by_space.setdefault(str(info.space_id), {})[info.role] = key
        rows = []
        for space_id in sorted(set(by_space) | set(meta)):
            ctx.checkpoint()
            roles = by_space.get(space_id, {})
            m = meta.get(space_id)
            row: dict = {"spaceId": space_id, "name": (m.name if m and m.name else space_id), "purpose": m.purpose if m else None,
                         "reasons": []}
            if {"temp", "humidity", "co2"} <= set(roles):
                sub = self._sub(data, {r: roles[r] for r in ("temp", "humidity", "co2")})
                res = COMFORT.run(sub, COMFORT.parse_params({"useSpaceTargets": False}), ctx)
                row["comfortScore"] = next(x.value for x in res.metrics if x.key == "score")
            else:
                row["comfortScore"] = None
                row["reasons"].append("쾌적도: 온도·습도·CO2 연결 없음")
            signals = {r: roles[r] for r in ("co2", "activity") if r in roles}
            if len(signals) >= 2:
                sub = self._sub(data, signals)
                res = OCCUPANCY.run(sub, OCCUPANCY.parse_params({}), ctx)
                row["utilizationPercent"] = next(x.value for x in res.metrics if x.key == "utilizationPercent")
            else:
                row["utilizationPercent"] = None
                row["reasons"].append("활용률: 재실 신호(CO2·활동) 부족")
            if "energy" in roles:
                kwh = float(data.role("energy")[roles["energy"]].sum())
                row["energyKwh"] = round(kwh, 6)
                if m and m.areaM2:
                    row["energyPerM2"] = round(kwh / m.areaM2, 6)
                else:
                    row["energyPerM2"] = None
                    row["reasons"].append("㎡당 에너지: 면적 없음")
            else:
                row["energyKwh"] = row["energyPerM2"] = None
                row["reasons"].append("㎡당 에너지: 전력량 연결 없음")
            rows.append(row)
        for metric, desc in (("comfortScore", True), ("utilizationPercent", True), ("energyPerM2", False)):
            groups: dict = {}
            for r in rows:
                if r[metric] is not None:
                    groups.setdefault(r["purpose"], []).append(r)
            for members in groups.values():
                members.sort(key=lambda r: (-r[metric] if desc else r[metric], r["spaceId"]))
                for i, r in enumerate(members, start=1):
                    r[metric + "Rank"] = i
            for r in rows:
                r.setdefault(metric + "Rank", None)
        for r in rows:
            r["reason"] = "; ".join(r.pop("reasons")) or None
        cols = [("name", "공간", "string"), ("purpose", "용도", "string"), ("comfortScore", "쾌적도", "number"),
                ("comfortScoreRank", "쾌적도 순위", "number"), ("utilizationPercent", "활용률(%)", "number"),
                ("utilizationPercentRank", "활용률 순위", "number"), ("energyPerM2", "㎡당 kWh", "number"),
                ("energyPerM2Rank", "에너지 순위(적을수록 1)", "number"), ("reason", "빠진 지표 사유", "string")]
        charts = []
        for metric, title in (("comfortScore", "쾌적도"), ("utilizationPercent", "활용률(%)"), ("energyPerM2", "㎡당 에너지(kWh)")):
            charts.append(chart(metric, "bar", f"공간별 {title}", x_axis={"type": "category", "label": "공간"}, y_axis={"type": "value", "label": title},
                                series=[{"key": metric, "label": title, "data": [[r["name"], r[metric]] for r in rows]}]))
        best = [r for r in rows if r.get("comfortScoreRank") == 1]
        headline = f"공간 {len(rows)}곳 비교" + (f": 쾌적도 1위 {best[0]['name']}" if best else "")
        return Result(headline=headline, metrics=[Metric("spaces", "공간 수", len(rows))], charts=charts,
                      tables=[table("ranking", "공간별 지표와 순위", cols, rows)], caveats=list(self.fixed_caveats),
                      provenance={"algorithm": "쾌적도·재실 규칙·에너지 합계 순위"})

    @staticmethod
    def _sub(data: InputData, roles: dict[str, str]) -> InputData:
        frames, infos = {}, {}
        for role, key in roles.items():
            src_role = data.series[key].role
            frames[role] = data.role(src_role)[[key]]
            i = data.series[key]
            infos[key] = SeriesInfo(key=key, role=role, label=i.label, kind=i.kind, device_id=i.device_id, space_id=i.space_id,
                                    metric_key=i.metric_key, unit=i.unit)
        return InputData(frames=frames, series=infos, period_from=data.period_from, period_to=data.period_to, resolution=data.resolution)


TEMPLATE = SpaceBenchmark()
