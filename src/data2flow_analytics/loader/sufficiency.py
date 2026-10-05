"""데이터 충분성 확인(ANA-03.03, ANA-08.01, BR-ANA-04·05)과 실행 한도(ANA-11.01, BR-ANA-07).

판정: 기간 < 최소 기간 또는 포인트 < 최소 포인트 또는 누락률 > 실패 기준이면 FAIL, 누락률 > 경고 기준이면 WARN, 나머지 OK.
누락률은 기기 보고 주기(수신 간격의 중앙값)로 기대 건수를 내서 계산한다(TC-ANA-087: 1분 주기 하루 1,440 기대, 1,224 수신 → 15%).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..templates.base import Requirements
from .loader import RES_SECONDS, SeriesStats

ORDER = ["RAW", "1m", "1h", "1d"]


@dataclass
class CheckResult:
    level: str
    issues: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    limits: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"level": self.level, "issues": self.issues, "stats": self.stats, "limits": self.limits}

    @property
    def reason(self) -> str:
        fails = [i["message"] for i in self.issues if i["severity"] == "FAIL"]
        return fails[0] if fails else ""


def estimate_points(stats: list[SeriesStats], resolution: str, period_sec: float) -> int:
    """집계 단위에서 읽게 될 포인트 수 추정. 원본은 받은 건수, 집계는 min(받은 건수, 기간 ÷ 단위)."""
    sec = RES_SECONDS.get(resolution)
    total = 0
    for st in stats:
        if sec is None:
            total += st.received
        else:
            buckets = int(period_sec // sec) + 1
            total += min(st.received, buckets) if len(st.devices) <= 1 else buckets
    return total


def effective_resolution(requested: str, req: Requirements, stats: list[SeriesStats], period_sec: float) -> str:
    """AUTO면 템플릿 권장 단위로 시작하고, 한도를 넘으면 한 단계씩 올린다."""
    if requested != "AUTO":
        return requested
    start = req.recommended_resolution if req.recommended_resolution in ORDER else "1h"
    for res in ORDER[ORDER.index(start):]:
        if estimate_points(stats, res, period_sec) <= req.max_points:
            return res
    return "1d"


def suggest_resolution(stats: list[SeriesStats], current: str, max_points: int, period_sec: float) -> str | None:
    for res in ORDER[ORDER.index(current) + 1 if current in ORDER else 0:]:
        if estimate_points(stats, res, period_sec) <= max_points:
            return res
    return None


def check(stats: list[SeriesStats], req: Requirements, period_from: datetime, period_to: datetime, resolution: str,
          include_virtual: bool, extra_issues: list[dict] | None = None, max_series: int | None = None) -> CheckResult:
    period_sec = (period_to - period_from).total_seconds()
    period_days = period_sec / 86400
    effective = effective_resolution(resolution, req, stats, period_sec)
    points = estimate_points(stats, effective, period_sec)
    received = sum(st.received for st in stats)
    expected = 0.0
    for st in stats:
        interval = st.interval_sec
        if not interval:
            interval = RES_SECONDS.get(effective) or 60.0
        expected += max(1, len(st.devices)) * period_sec / interval
    missing = max(0.0, 1 - received / expected) if expected else 1.0
    spans = [(st.last - st.first).total_seconds() / 86400 if st.received and st.first and st.last else 0.0 for st in stats]
    data_days = min(spans) if spans else 0.0
    quality: dict[str, int] = {str(q): 0 for q in range(5)}
    for st in stats:
        for q, n in st.quality.items():
            quality[str(q)] = quality.get(str(q), 0) + n
    issues: list[dict] = []
    level = "OK"

    def add(code: str, severity: str, message: str, fix: dict | None = None) -> None:
        nonlocal level
        issue = {"code": code, "severity": severity, "message": message}
        if fix:
            issue["fix"] = fix
        issues.append(issue)
        if severity == "FAIL":
            level = "FAIL"
        elif severity == "WARN" and level == "OK":
            level = "WARN"

    shown_days = min(period_days, data_days)
    if period_days + 1e-9 < req.min_period_days or data_days + 1 / 24 < req.min_period_days:
        add("PERIOD_TOO_SHORT", "FAIL", f"최소 {_days(req.min_period_days)}일 데이터가 필요합니다(현재 {_days(shown_days)}일)",
            {"type": "SET_PERIOD_DAYS", "value": req.min_period_days})
    if received < req.min_points:
        add("TOO_FEW_POINTS", "FAIL", f"최소 {req.min_points}개 포인트가 필요합니다(현재 {received}개)")
    if missing > req.missing_fail + 1e-12:
        add("MISSING_TOO_HIGH", "FAIL", f"누락률 {missing * 100:.1f}%가 실패 기준 {req.missing_fail * 100:.0f}%를 넘습니다")
    elif missing > req.missing_warn + 1e-12:
        add("MISSING_HIGH", "WARN", f"누락률 {missing * 100:.1f}%가 경고 기준 {req.missing_warn * 100:.0f}%를 넘습니다")
    limit_series = max_series or req.max_series
    if len(stats) > limit_series:
        add("TOO_MANY_SERIES", "FAIL", f"입력 시계열은 {limit_series}개까지입니다(현재 {len(stats)}개)")
    if points > req.max_points:
        sug = suggest_resolution(stats, effective, req.max_points, period_sec)
        add("TOO_MANY_POINTS", "FAIL", f"예상 포인트 {points:,}개가 한도 {req.max_points:,}개를 넘습니다",
            {"type": "SET_RESOLUTION", "value": sug} if sug else None)
    if req.max_period_days and period_days > req.max_period_days + 1e-9:
        add("PERIOD_TOO_LONG", "FAIL", f"이 템플릿은 최대 {req.max_period_days}일까지 실행합니다",
            {"type": "SET_PERIOD_DAYS", "value": req.max_period_days})
    for issue in extra_issues or []:
        add(issue["code"], issue["severity"], issue["message"], issue.get("fix"))
    stats_json = {
        "points": points,
        "estimatedRawPoints": received,
        "missingRate": round(missing, 6),
        "qualityDistribution": quality,
        "virtualPoints": sum(st.virtual_points for st in stats) if include_virtual else 0,
        "seriesCount": len(stats),
        "effectiveResolution": effective,
    }
    limits = {"maxPoints": req.max_points, "maxSeries": limit_series, "timeoutSec": req.timeout_sec}
    return CheckResult(level=level, issues=issues, stats=stats_json, limits=limits)


def limit_violation(result: CheckResult) -> dict | None:
    """BR-ANA-07 위반(시계열 수·포인트 수)이면 해당 이슈."""
    for issue in result.issues:
        if issue["code"] in ("TOO_MANY_SERIES", "TOO_MANY_POINTS", "PERIOD_TOO_LONG"):
            return issue
    return None


def _days(v: float) -> str:
    return f"{v:.0f}" if abs(v - round(v)) < 0.05 or v >= 1 else f"{v:.2f}"
