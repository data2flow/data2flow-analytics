-- ENE-02 가상 센서 일 계산 기록: 공간·종류·날짜마다 한 번만 계산하고 EVT-ENE-03을 한 번만 낸다.
-- ACH는 감쇠 구간이 없으면 ach_estimates 행이 없으므로, 계산했다는 사실과 사유를 여기에 남긴다
CREATE TABLE data2flow_analytics.virtual_sensor_runs (
  organization_id bigint      NOT NULL,
  space_id        bigint      NOT NULL,
  kind            varchar(16) NOT NULL,
  day             date        NOT NULL,
  status          varchar(16) NOT NULL,
  reason          varchar(32),
  computed_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_virtual_sensor_runs PRIMARY KEY (organization_id, space_id, kind, day),
  CONSTRAINT ck_virtual_sensor_runs_kind CHECK (kind IN ('ACH', 'OCCUPANCY', 'THERMAL_COMFORT')),
  CONSTRAINT ck_virtual_sensor_runs_status CHECK (status IN ('OK', 'INPUT_MISSING'))
);
COMMENT ON TABLE data2flow_analytics.virtual_sensor_runs IS '가상 센서 일 계산 기록(ENE-02). reason: 추정하지 않은 사유(LOW_AMPLITUDE, TOO_SHORT, LOW_FIT, NO_DATA)';
