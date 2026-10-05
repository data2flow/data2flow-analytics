"""분석 일정(ANA-04.01): cron·프리셋 검증과 다음 실행 시각(조직 시간대)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from data2flow_analytics.analysis.schedule import Cron, next_run, validate
from data2flow_analytics.common.errors import BusinessError

NOW = datetime(2026, 10, 4, 20, 59, 59, tzinfo=UTC)  # 2026-10-05 05:59:59 KST(월)


@pytest.mark.spec("ANA-04.01")
@pytest.mark.parametrize(("schedule", "expected"), [
    ({"cron": "0 0 6 * * MON"}, "2026-10-04T21:00:00+00:00"),
    ({"cron": "0 6 * * 1"}, "2026-10-04T21:00:00+00:00"),
    ({"preset": "DAILY", "at": "06:00"}, "2026-10-04T21:00:00+00:00"),
    ({"preset": "WEEKLY", "at": "06:00", "weekday": "TUE"}, "2026-10-05T21:00:00+00:00"),
    ({"preset": "WEEKLY", "at": "09:30", "weekday": 3}, "2026-10-07T00:30:00+00:00"),
    ({"cron": "*/15 9-17 * * MON-FRI"}, "2026-10-05T00:00:00+00:00"),
    ({"cron": "0 0 1 1 *"}, "2026-12-31T15:00:00+00:00"),
    ({"cron": "0 12 15 * *", "timezone": "UTC"}, "2026-10-15T12:00:00+00:00"),
])
def test_ANA_04_01_next_run_in_org_timezone(schedule, expected):
    """[ANA-04.01][TC-ANA-095] cron·DAILY·WEEKLY 프리셋의 다음 실행 시각을 Asia/Seoul로 계산해 UTC로 저장"""
    assert next_run(schedule, NOW).isoformat() == expected


@pytest.mark.spec("ANA-04.01")
@pytest.mark.parametrize("bad", [{"cron": "0 61 * * *"}, {"cron": "* * *"}, {"cron": "0 0 32 * *"}, {"cron": "0 0 * * FOO"},
                                 {"cron": "0 0/0 * * *"}, {"cron": "0 5-1 * * *"}, {"preset": "HOURLY"}, {"preset": "DAILY", "at": "25:00"},
                                 {"preset": "WEEKLY", "weekday": "XYZ"}, {"preset": "DAILY", "at": "x"}, "nope"])
def test_ANA_04_01_TC_ANA_097_invalid(bad):
    """[ANA-04.01][TC-ANA-097] 잘못된 식 → ANALYSIS_SCHEDULE_INVALID(400)"""
    with pytest.raises(BusinessError) as exc:
        validate(bad) if not isinstance(bad, str) else Cron(bad)
    assert exc.value.code.name == "ANALYSIS_SCHEDULE_INVALID" and exc.value.code.status == 400


def test_no_schedule():
    assert next_run(None, NOW) is None
    validate(None)
