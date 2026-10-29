-- ENE-01 에너지 일 집계 확정 기록: 조직·날짜마다 한 번만 쓰고 EVT-ENE-01을 한 번만 낸다(TC-ENE-006).
-- 서버가 여러 대여도 조직별 advisory lock 안에서 이 행을 먼저 넣은 인스턴스만 쓴다
CREATE TABLE data2flow_analytics.energy_daily_runs (
  organization_id bigint      NOT NULL,
  day             date        NOT NULL,
  space_count     integer     NOT NULL DEFAULT 0,
  computed_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_energy_daily_runs PRIMARY KEY (organization_id, day)
);
COMMENT ON TABLE data2flow_analytics.energy_daily_runs IS '에너지 일 집계 확정 기록(02:00 작업). 다시 계산(API-ENE-80)은 이 행과 상관없이 쓴다';
