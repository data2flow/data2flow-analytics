-- =============================================================================
-- data2flow_pipeline 초기 스키마(PostgreSQL 18). 정본: data2flow-docs design/erd/pipeline.md, 초안 design/erd/ddl/20-pipeline.sql
-- 소유: data2flow-pipeline(쓰기는 pipeline만). 읽기 허용: core-api(조회), analytics(시계열)
-- 규칙: 테이블 복수형(집합 명사 제외), BIGINT IDENTITY, timestamptz(UTC), 스키마 사이 FK 없음
-- 파티션: 부모 + DEFAULT 파티션만 여기서 만든다. 날짜별 파티션은 pipeline 스케줄러(PartitionMaintenanceService)가
--         미리 만들고 지운다(ADR-019, BR-TSD-01·02). pg_partman은 쓰지 않는다.
-- ADR-030: staging 배포 때만 migrate, prod는 validate. 이 파일은 확장(추가)만 한다.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- RawMessage
-- -----------------------------------------------------------------------------
CREATE TABLE raw_messages (
    id                bigint GENERATED ALWAYS AS IDENTITY,
    organization_id   bigint       NOT NULL,
    source_id         bigint       NOT NULL,
    device_id         bigint,
    message_id        uuid         NOT NULL,
    source_type       varchar(16)  NOT NULL,
    topic             varchar(512),
    payload           bytea        NOT NULL,
    payload_encoding  varchar(8)   NOT NULL,
    ingress_instance  varchar(64)  NOT NULL,
    dedup_key         varchar(128) NOT NULL,
    stream_partition  smallint     NOT NULL,
    stream_offset     bigint       NOT NULL,
    external_id       varchar(128),
    status            varchar(32)  NOT NULL DEFAULT 'RECEIVED',
    error_code        varchar(64),
    error_detail      jsonb,
    processing_trace  jsonb,
    metric_count      smallint,
    dropped           boolean      NOT NULL DEFAULT false,
    is_virtual        boolean      NOT NULL DEFAULT false,
    received_at       timestamptz  NOT NULL,
    processed_at      timestamptz,
    CONSTRAINT pk_raw_messages PRIMARY KEY (id, received_at),
    -- RawEnvelope.sourceType = DSC data_sources.type(계약 SourceTypes 13종이 정본). 모르는 값은 pipeline이 INVALID로 남긴다
    CONSTRAINT ck_raw_messages_source_type CHECK (source_type IN ('MQTT_SUBSCRIBE','PLATFORM_BROKER','WEBHOOK','SIMULATION',
        'CONNECTOR','EDGE','KMA_WEATHER','AIRKOREA','HOLIDAY','ICAL','ONEM2M','OPCUA','MODBUS_TCP')),
    CONSTRAINT ck_raw_messages_payload_encoding CHECK (payload_encoding IN ('JSON','TEXT','BINARY')),
    CONSTRAINT ck_raw_messages_payload_size CHECK (octet_length(payload) <= 262144),
    CONSTRAINT ck_raw_messages_stream_partition CHECK (stream_partition BETWEEN -1 AND 255),
    CONSTRAINT ck_raw_messages_status CHECK (status IN ('RECEIVED','OK','DUPLICATE','DECODE_ERROR','SCRIPT_ERROR',
        'UNKNOWN_DEVICE_REJECTED','INVALID','STORE_ERROR','PUBLISH_ERROR'))
) PARTITION BY RANGE (received_at);

CREATE TABLE raw_messages_default PARTITION OF raw_messages DEFAULT;

CREATE INDEX ix_raw_messages_org_dedup_key ON raw_messages (organization_id, dedup_key, received_at DESC);
CREATE INDEX ix_raw_messages_org_device_received ON raw_messages (organization_id, device_id, received_at DESC);
CREATE INDEX ix_raw_messages_failed ON raw_messages (organization_id, received_at DESC)
    WHERE status NOT IN ('OK','DUPLICATE','RECEIVED');
CREATE INDEX ix_raw_messages_message_id ON raw_messages (message_id);

COMMENT ON TABLE raw_messages IS '수신 원본. 처리 결과와 함께 한 트랜잭션으로 기록(BR-ING-01). 일 파티션, 30일 보관(BR-ING-14)';
COMMENT ON COLUMN raw_messages.dedup_key IS '중복 판정 키. 최근 10분 창(BR-ING-07). 값만 있는 payload는 수신 시각 버킷 키(sha256b:)';
COMMENT ON COLUMN raw_messages.message_id IS 'RawEnvelope.messageId. 같은 스트림 메시지를 다시 읽었는지(재발행) 판단';
COMMENT ON COLUMN raw_messages.source_type IS 'RawEnvelope.sourceType = DSC data_sources.type(SourceTypes)';

CREATE TABLE dlq_items (
    id               bigint GENERATED ALWAYS AS IDENTITY,
    organization_id  bigint       NOT NULL,
    raw_message_id   bigint       NOT NULL,
    raw_received_at  timestamptz  NOT NULL,
    source_id        bigint       NOT NULL,
    device_id        bigint,
    stage            varchar(16)  NOT NULL,
    error_code       varchar(64)  NOT NULL,
    error_message    text         NOT NULL,
    attempts         smallint     NOT NULL DEFAULT 0,
    status           varchar(16)  NOT NULL DEFAULT 'OPEN',
    locked_by        bigint,
    locked_until     timestamptz,
    discard_reason   varchar(200),
    created_at       timestamptz  NOT NULL DEFAULT now(),
    resolved_at      timestamptz,
    CONSTRAINT pk_dlq_items PRIMARY KEY (id),
    CONSTRAINT ck_dlq_items_stage CHECK (stage IN ('DECODE','SCRIPT','STORE','PUBLISH')),
    CONSTRAINT ck_dlq_items_status CHECK (status IN ('OPEN','REPROCESSING','RESOLVED','DISCARDED'))
);
CREATE INDEX ix_dlq_items_org_status_created ON dlq_items (organization_id, status, created_at DESC);
CREATE UNIQUE INDEX uq_dlq_items_raw_message_open ON dlq_items (raw_message_id)
    WHERE status IN ('OPEN','REPROCESSING');
COMMENT ON TABLE dlq_items IS '처리 실패 메시지. raw_messages와 FK 없음(파티션 삭제 때문). 14일 보관';

CREATE TABLE reprocess_jobs (
    id               bigint GENERATED ALWAYS AS IDENTITY,
    organization_id  bigint       NOT NULL,
    source_id        bigint       NOT NULL,
    device_ids       bigint[],
    period_from      timestamptz  NOT NULL,
    period_to        timestamptz  NOT NULL,
    status           varchar(16)  NOT NULL DEFAULT 'PENDING',
    total            bigint       NOT NULL DEFAULT 0,
    processed        bigint       NOT NULL DEFAULT 0,
    failed           bigint       NOT NULL DEFAULT 0,
    decoder_version  varchar(32)  NOT NULL,
    script_versions  jsonb        NOT NULL DEFAULT '{}'::jsonb,
    requested_by     bigint       NOT NULL,
    memo             varchar(200),
    created_at       timestamptz  NOT NULL DEFAULT now(),
    started_at       timestamptz,
    finished_at      timestamptz,
    CONSTRAINT pk_reprocess_jobs PRIMARY KEY (id),
    CONSTRAINT ck_reprocess_jobs_status CHECK (status IN ('PENDING','RUNNING','COMPLETED','FAILED','CANCELLED')),
    CONSTRAINT ck_reprocess_jobs_period CHECK (period_to > period_from AND period_to - period_from <= interval '31 days')
);
CREATE UNIQUE INDEX uq_reprocess_jobs_source_running ON reprocess_jobs (source_id)
    WHERE status IN ('PENDING','RUNNING');
CREATE INDEX ix_reprocess_jobs_org_created ON reprocess_jobs (organization_id, created_at DESC);
COMMENT ON TABLE reprocess_jobs IS '과거 원본 재처리(ING-01.04, M5). 소스당 동시 1건, 시작 시점 버전 고정(BR-ING-12)';

-- -----------------------------------------------------------------------------
-- Telemetry
-- -----------------------------------------------------------------------------
CREATE TABLE telemetry (
    device_id        bigint            NOT NULL,
    metric_key       varchar(64)       NOT NULL,
    time             timestamptz       NOT NULL,
    organization_id  bigint            NOT NULL,
    value            double precision  NOT NULL,
    quality          smallint          NOT NULL DEFAULT 0,
    flags            smallint          NOT NULL DEFAULT 0,
    is_virtual       boolean           NOT NULL DEFAULT false,
    received_at      timestamptz       NOT NULL,
    raw_message_id   bigint,
    CONSTRAINT pk_telemetry PRIMARY KEY (device_id, metric_key, time),
    CONSTRAINT ck_telemetry_quality CHECK (quality BETWEEN 0 AND 5),
    CONSTRAINT ck_telemetry_value_finite CHECK (value NOT IN ('NaN'::float8, 'Infinity'::float8, '-Infinity'::float8))
) PARTITION BY RANGE (time);

CREATE TABLE telemetry_default PARTITION OF telemetry DEFAULT;

CREATE INDEX ix_telemetry_device_metric_time ON telemetry (device_id, metric_key, time DESC);
CREATE INDEX ix_telemetry_time_brin ON telemetry USING brin (time);
COMMENT ON TABLE telemetry IS '측정값 원본(긴 형식, BR-TSD-24). 월 파티션 + DEFAULT(BR-TSD-01)';
COMMENT ON COLUMN telemetry.flags IS '비트: 1 late, 2 imported, 4 reprocessed, 8 state_change';
COMMENT ON COLUMN telemetry.quality IS '0 정상, 1 범위 초과, 2 미검증, 3 의심, 4 시각 보정, 5 예보';

CREATE TABLE telemetry_1m (
    device_id        bigint            NOT NULL,
    metric_key       varchar(64)       NOT NULL,
    bucket           timestamptz       NOT NULL,
    organization_id  bigint            NOT NULL,
    count            integer           NOT NULL,
    count_all        integer           NOT NULL,
    avg              double precision,
    min              double precision,
    max              double precision,
    sum              double precision,
    first            double precision,
    last             double precision,
    first_time       timestamptz,
    last_time        timestamptz,
    twa              double precision,
    state_on_sec     integer,
    state_changes    integer,
    is_virtual       boolean           NOT NULL DEFAULT false,
    CONSTRAINT pk_telemetry_1m PRIMARY KEY (device_id, metric_key, bucket)
) PARTITION BY RANGE (bucket);
CREATE TABLE telemetry_1m_default PARTITION OF telemetry_1m DEFAULT;
CREATE INDEX ix_telemetry_1m_org_bucket ON telemetry_1m (organization_id, bucket);

CREATE TABLE telemetry_1h (LIKE telemetry_1m INCLUDING DEFAULTS,
    CONSTRAINT pk_telemetry_1h PRIMARY KEY (device_id, metric_key, bucket)
) PARTITION BY RANGE (bucket);
CREATE TABLE telemetry_1h_default PARTITION OF telemetry_1h DEFAULT;
CREATE INDEX ix_telemetry_1h_org_bucket ON telemetry_1h (organization_id, bucket);

CREATE TABLE telemetry_1d (LIKE telemetry_1m INCLUDING DEFAULTS,
    CONSTRAINT pk_telemetry_1d PRIMARY KEY (device_id, metric_key, bucket)
);
CREATE INDEX ix_telemetry_1d_org_bucket ON telemetry_1d (organization_id, bucket);
COMMENT ON COLUMN telemetry_1d.bucket IS '기기가 속한 사이트 시간대 자정(BR-TSD-05)';

CREATE TABLE link_qualities (
    device_id        bigint        NOT NULL,
    gateway_eui      varchar(32)   NOT NULL DEFAULT '',
    time             timestamptz   NOT NULL,
    organization_id  bigint        NOT NULL,
    rssi             numeric(6,2),
    snr              numeric(6,2),
    f_cnt            bigint,
    frequency        bigint,
    data_rate        varchar(12),
    CONSTRAINT pk_link_qualities PRIMARY KEY (device_id, gateway_eui, time)
) PARTITION BY RANGE (time);
CREATE TABLE link_qualities_default PARTITION OF link_qualities DEFAULT;
COMMENT ON TABLE link_qualities IS '게이트웨이별 신호. 월 파티션, 90일 보관(TSD-01.02)';

CREATE TABLE agg_watermarks (
    level            varchar(4)   NOT NULL,
    processed_until  timestamptz  NOT NULL,
    updated_at       timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT pk_agg_watermarks PRIMARY KEY (level),
    CONSTRAINT ck_agg_watermarks_level CHECK (level IN ('1m','1h','1d'))
);

CREATE TABLE agg_dirty_ranges (
    id               bigint GENERATED ALWAYS AS IDENTITY,
    organization_id  bigint       NOT NULL,
    level            varchar(4)   NOT NULL,
    device_id        bigint       NOT NULL,
    metric_key       varchar(64)  NOT NULL,
    from_ts          timestamptz  NOT NULL,
    to_ts            timestamptz  NOT NULL,
    reason           varchar(10)  NOT NULL,
    created_at       timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT pk_agg_dirty_ranges PRIMARY KEY (id),
    CONSTRAINT ck_agg_dirty_ranges_level CHECK (level IN ('1m','1h','1d')),
    CONSTRAINT ck_agg_dirty_ranges_reason CHECK (reason IN ('LATE','REPROCESS','IMPORT','REMAP','DELETE')),
    CONSTRAINT ck_agg_dirty_ranges_range CHECK (to_ts > from_ts)
);
CREATE INDEX ix_agg_dirty_ranges_level_created ON agg_dirty_ranges (level, created_at);
COMMENT ON TABLE agg_dirty_ranges IS '다시 계산할 집계 구간(BR-TSD-06). 처리 후 삭제';

CREATE TABLE partition_registries (
    partition_name  varchar(63)  NOT NULL,
    table_name      varchar(63)  NOT NULL,
    range_from      timestamptz  NOT NULL,
    range_to        timestamptz  NOT NULL,
    state           varchar(12)  NOT NULL DEFAULT 'CREATED',
    rows_estimate   bigint,
    bytes           bigint,
    updated_at      timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT pk_partition_registries PRIMARY KEY (partition_name),
    CONSTRAINT ck_partition_registries_state CHECK (state IN ('CREATED','COMPRESSED','ARCHIVED','DROPPED'))
);
CREATE INDEX ix_partition_registries_table ON partition_registries (table_name, range_from);

-- -----------------------------------------------------------------------------
-- DeviceState (device_snapshot은 여기로 통합 — design/erd/pipeline.md §4.2)
-- -----------------------------------------------------------------------------
CREATE TABLE device_state (
    device_id                bigint        NOT NULL,
    organization_id          bigint        NOT NULL,
    last_seen_at             timestamptz,
    last_measured_at         timestamptz,
    connectivity             varchar(8)    NOT NULL DEFAULT 'UNKNOWN',
    connectivity_changed_at  timestamptz,
    latest                   jsonb         NOT NULL DEFAULT '{}'::jsonb,
    battery                  numeric(5,2),
    rssi                     numeric(6,2),
    snr                      numeric(6,2),
    best_gateway_eui         varchar(32),
    clock_skew_avg_sec       integer,
    msg_count_24h            integer       NOT NULL DEFAULT 0,
    updated_at               timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT pk_device_state PRIMARY KEY (device_id),
    CONSTRAINT ck_device_state_connectivity CHECK (connectivity IN ('UNKNOWN','ONLINE','OFFLINE'))
);
CREATE INDEX ix_device_state_org_connectivity ON device_state (organization_id, connectivity);
CREATE INDEX ix_device_state_last_seen ON device_state (last_seen_at);
COMMENT ON TABLE device_state IS '기기 실시간 상태(기기당 1행). core-api는 읽기만. 더 최신 측정일 때만 갱신(BR-ING-08)';

-- -----------------------------------------------------------------------------
-- DataQuality
-- -----------------------------------------------------------------------------
CREATE TABLE data_quality_daily (
    device_id           bigint       NOT NULL,
    day                 date         NOT NULL,
    organization_id     bigint       NOT NULL,
    score               smallint     NOT NULL,
    completeness        smallint     NOT NULL,
    timeliness          smallint     NOT NULL,
    validity            smallint     NOT NULL,
    stability           smallint     NOT NULL,
    expected_count      integer      NOT NULL DEFAULT 0,
    received_count      integer      NOT NULL DEFAULT 0,
    late_count          integer      NOT NULL DEFAULT 0,
    out_of_range_count  integer      NOT NULL DEFAULT 0,
    suspect_count       integer      NOT NULL DEFAULT 0,
    created_at          timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT pk_data_quality_daily PRIMARY KEY (device_id, day),
    CONSTRAINT ck_data_quality_daily_score CHECK (score BETWEEN 0 AND 100
        AND completeness BETWEEN 0 AND 100 AND timeliness BETWEEN 0 AND 100
        AND validity BETWEEN 0 AND 100 AND stability BETWEEN 0 AND 100)
);
CREATE INDEX ix_data_quality_daily_org_day ON data_quality_daily (organization_id, day);

CREATE TABLE data_gaps (
    id               bigint GENERATED ALWAYS AS IDENTITY,
    organization_id  bigint       NOT NULL,
    device_id        bigint       NOT NULL,
    gap_start        timestamptz  NOT NULL,
    gap_end          timestamptz  NOT NULL,
    expected_count   integer      NOT NULL,
    detected_at      timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT pk_data_gaps PRIMARY KEY (id),
    CONSTRAINT ck_data_gaps_range CHECK (gap_end > gap_start)
);
CREATE UNIQUE INDEX uq_data_gaps_device_start ON data_gaps (device_id, gap_start);
CREATE INDEX ix_data_gaps_device_start ON data_gaps (device_id, gap_start DESC);
CREATE INDEX ix_data_gaps_org_start ON data_gaps (organization_id, gap_start DESC);
COMMENT ON TABLE data_gaps IS '수신 공백. 예상 주기의 3배 이상(BR-ING-17)';

-- -----------------------------------------------------------------------------
-- 스케줄 작업 잠금(ShedLock, design/oss-stack.md). 작업 이름마다 한 인스턴스만 실행(시계열 유지·오프라인 판정)
-- staging·prod가 DB를 함께 쓰므로(ADR-030) 두 환경 사이에서도 한 번만 실행된다
-- -----------------------------------------------------------------------------
CREATE TABLE scheduler_locks (
    name        varchar(64)   NOT NULL,
    lock_until  timestamptz   NOT NULL,
    locked_at   timestamptz   NOT NULL,
    locked_by   varchar(255)  NOT NULL,
    CONSTRAINT pk_scheduler_locks PRIMARY KEY (name)
);
COMMENT ON TABLE scheduler_locks IS 'ShedLock 작업 잠금(표준 테이블). 작업 이름당 1행';
