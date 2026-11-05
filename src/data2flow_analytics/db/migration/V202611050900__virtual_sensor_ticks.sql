-- ENE-02.02 재실 추정 가상 센서: 5분 주기 계산의 다중 인스턴스 중복 방지(공간·종류·구간마다 먼저 넣은 인스턴스만 계산)
CREATE TABLE data2flow_analytics.virtual_sensor_ticks (
    organization_id bigint NOT NULL,
    space_id bigint NOT NULL,
    kind varchar(16) NOT NULL,
    bucket timestamptz NOT NULL,
    status varchar(16) NOT NULL,
    value double precision,
    sent boolean NOT NULL DEFAULT false,
    computed_at timestamptz NOT NULL,
    CONSTRAINT pk_virtual_sensor_ticks PRIMARY KEY (organization_id, space_id, kind, bucket),
    CONSTRAINT ck_virtual_sensor_ticks_status CHECK (status IN ('OK', 'INPUT_MISSING'))
);
COMMENT ON TABLE data2flow_analytics.virtual_sensor_ticks IS '가상 센서 실시간 계산 기록(ENE-02.02 재실 추정 5분). 7일 지나면 지운다';
COMMENT ON COLUMN data2flow_analytics.virtual_sensor_ticks.sent IS 'pipeline API-ING-25로 값을 넘겼는가';
