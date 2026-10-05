"""실행 상태 전이(domain-model Run.status)."""

from __future__ import annotations

import pytest

from data2flow_analytics.analysis.runs import TRANSITIONS, can_transition, ensure_transition
from data2flow_analytics.common.errors import BusinessError

STATES = ["QUEUED", "PENDING", "RUNNING", "SUCCEEDED", "FAILED", "TIMEOUT", "CANCELLED"]
ALLOWED = {("QUEUED", "PENDING"), ("QUEUED", "CANCELLED"), ("PENDING", "RUNNING"), ("PENDING", "CANCELLED"), ("PENDING", "QUEUED"),
           ("RUNNING", "SUCCEEDED"), ("RUNNING", "FAILED"), ("RUNNING", "TIMEOUT"), ("RUNNING", "CANCELLED"), ("RUNNING", "PENDING")}


@pytest.mark.spec("ANA-04.02")
@pytest.mark.parametrize("current", STATES)
@pytest.mark.parametrize("target", STATES)
def test_ANA_04_02_TC_ANA_100_state_matrix(current, target):
    """[ANA-04.02][AT-ANA-02.1] 상태×이벤트 표 전체. 끝 상태에서는 모든 전이 거부(ANALYSIS_RUN_STATE_CONFLICT)"""
    assert can_transition(current, target) == ((current, target) in ALLOWED)
    if (current, target) not in ALLOWED:
        with pytest.raises(BusinessError) as exc:
            ensure_transition(current, target)
        assert exc.value.code.name == "ANALYSIS_RUN_STATE_CONFLICT"
    assert set(TRANSITIONS) == set(STATES)
