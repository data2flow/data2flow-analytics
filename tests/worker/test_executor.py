"""실행기: 시간 제한(ANA-04.03, BR-ANA-09)과 협조적 취소(ANA-04.04)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from data2flow_analytics.common.errors import BusinessError
from data2flow_analytics.templates.base import CancelledError
from data2flow_analytics.templates.registry import BUILTIN_DIR, build_registry
from data2flow_analytics.worker.executor import InlineExecutor, Job, RunTimeoutError, SubprocessExecutor, TemplateFailure
from tests.fixtures import synth
from tests.fixtures.inputs import NOW, make_input

SLOW = textwrap.dedent('''
    from data2flow_analytics.templates.builtin.pattern_heatmap.template import PatternHeatmap
    from data2flow_analytics.templates.base import Requirements
    from data2flow_analytics.common.errors import BusinessError, ErrorCode

    class Slow(PatternHeatmap):
        key = "test-slow"
        requirements = Requirements(min_period_days=1, min_points=1, timeout_sec=2)

        def run(self, data, params, ctx):
            if data.extras.get("mode") == "fail":
                raise ValueError("boom")
            if data.extras.get("mode") == "business":
                raise BusinessError(ErrorCode.ANALYSIS_INSUFFICIENT_DATA, {"reason": "테스트"})
            if data.extras.get("mode") == "fast":
                return super().run(data, params, ctx)
            total = 0
            while True:  # 끝나지 않는 계산(대기 없이 CPU를 쓴다)
                ctx.checkpoint()
                total += sum(range(20000))

    TEMPLATE = Slow()
''')


@pytest.fixture(scope="module")
def slow_dir(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("tpl") / "test_slow"
    d.mkdir()
    (d / "template.py").write_text(SLOW, encoding="utf-8")
    (d / "GUIDE.md").write_text((BUILTIN_DIR / "pattern_heatmap" / "GUIDE.md").read_text(encoding="utf-8"), encoding="utf-8")
    return d.parent


def _job(mode: str) -> Job:
    data = make_input({"target": {"x": synth.series(1, days=2, step="1h").series}}, extras={"mode": mode})
    return Job("test-slow", "1.0.0", data, {}, 1, NOW, "Asia/Seoul")


@pytest.mark.spec("ANA-04.03")
def test_ANA_04_03_TC_ANA_107_timeout_kills_process(slow_dir):
    """[ANA-04.03][AT-ANA-15.1] 제한 2초인 test-slow → 2초 뒤 계산 프로세스 종료, RunTimeoutError(실행 TIMEOUT·ANALYSIS_TIMEOUT)"""
    ex = SubprocessExecutor([slow_dir], poll_sec=0.1)
    with pytest.raises(RunTimeoutError):
        ex.execute(_job("slow"), 2, lambda: False)


@pytest.mark.spec("ANA-04.04")
def test_ANA_04_04_TC_ANA_110_cancel(slow_dir):
    """[ANA-04.04][AT-ANA-04.1] RUNNING 취소 → 다음 체크포인트(최대 1초)에서 중단, 부분 결과 없음"""
    calls = {"n": 0}

    def cancel() -> bool:
        calls["n"] += 1
        return calls["n"] >= 2

    with pytest.raises(CancelledError):
        SubprocessExecutor([slow_dir], poll_sec=0.1).execute(_job("slow"), 30, cancel)
    registry = build_registry([slow_dir])
    with pytest.raises(CancelledError):
        InlineExecutor(registry).execute(_job("slow"), 30, lambda: True)
    with pytest.raises(RunTimeoutError):
        InlineExecutor(registry).execute(_job("slow"), 0.2, lambda: False)


def test_subprocess_results_and_errors(slow_dir):
    """하위 프로세스 결과 전달: 성공·업무 오류·예상 못한 오류(기술 로그는 detail)"""
    ex = SubprocessExecutor([slow_dir], poll_sec=0.1)
    assert ex.execute(_job("fast"), 30, lambda: False)["summary"]["headline"]
    with pytest.raises(BusinessError) as exc:
        ex.execute(_job("business"), 30, lambda: False)
    assert exc.value.code.name == "ANALYSIS_INSUFFICIENT_DATA"
    with pytest.raises(TemplateFailure) as failure:
        ex.execute(_job("fail"), 30, lambda: False)
    assert "boom" in str(failure.value) and "Traceback" in failure.value.detail
    with pytest.raises(TemplateFailure):
        InlineExecutor(build_registry([slow_dir])).execute(_job("fail"), 30, lambda: False)
