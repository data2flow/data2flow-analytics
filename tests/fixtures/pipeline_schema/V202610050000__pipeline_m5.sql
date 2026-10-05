-- =============================================================================
-- data2flow_pipeline M5(데이터 관리) 확장. 정본: data2flow-docs design/erd/pipeline.md §8(M5 추가분)
-- ADR-030: staging 배포 때만 migrate, prod는 validate. 이 파일은 확장(추가)만 한다 — 열 추가는 모두 NULL 허용이거나 기본값이 있고,
--          기존 열·제약은 바꾸지 않는다(이전 버전 pipeline이 같은 DB에서 그대로 돈다).
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 재처리(ING-01.04, BR-ING-12·13, ADR-042 메모)
-- -----------------------------------------------------------------------------
-- 플랫폼 브로커 서명 판정 결과(VERIFIED·UNSIGNED·INVALID)를 원본과 함께 보관한다. 재처리할 때 VERIFIED였던 원본은 quality 0을 유지하고,
-- 값이 없는(M5 이전) 원본만 미검증(quality 2)으로 다시 저장한다
ALTER TABLE raw_messages ADD COLUMN signature_status varchar(8);
ALTER TABLE raw_messages ADD CONSTRAINT ck_raw_messages_signature_status
    CHECK (signature_status IS NULL OR signature_status IN ('VERIFIED','UNSIGNED','INVALID'));
COMMENT ON COLUMN raw_messages.signature_status IS '플랫폼 브로커 서명 판정(RawEnvelope.signatureStatus, DSC-03.03). 재처리 품질 판단(ADR-042)';

-- 작업을 맡은 인스턴스와 생존 신호. 맡은 인스턴스가 죽어 heartbeat_at이 오래되면 다른 인스턴스가 넘겨받아 last_raw_id 다음부터 잇는다.
-- pinned_bundle은 작업을 만들 때 고정한 디코더·스크립트·모듈·수식 버전 묶음(BR-ING-12) — 처리 중 새 버전이 배포되어도 작업은 이 묶음으로 돈다
ALTER TABLE reprocess_jobs ADD COLUMN only_failed boolean NOT NULL DEFAULT false;
ALTER TABLE reprocess_jobs ADD COLUMN skipped bigint NOT NULL DEFAULT 0;
ALTER TABLE reprocess_jobs ADD COLUMN last_raw_id bigint NOT NULL DEFAULT 0;
ALTER TABLE reprocess_jobs ADD COLUMN owner_instance varchar(64);
ALTER TABLE reprocess_jobs ADD COLUMN heartbeat_at timestamptz;
ALTER TABLE reprocess_jobs ADD COLUMN pinned_bundle jsonb;
ALTER TABLE reprocess_jobs ADD COLUMN error varchar(500);
CREATE INDEX ix_reprocess_jobs_status_heartbeat ON reprocess_jobs (status, heartbeat_at) WHERE status IN ('PENDING','RUNNING');
COMMENT ON COLUMN reprocess_jobs.pinned_bundle IS '작업 생성 시점에 고정한 스크립트 실행 번들(BR-ING-12)';
COMMENT ON COLUMN reprocess_jobs.heartbeat_at IS '맡은 인스턴스의 생존 신호(10초). 2분 넘게 멈추면 다른 인스턴스가 넘겨받음';

-- -----------------------------------------------------------------------------
-- 기기 상태: 사이트 시간대(BR-TSD-05, 1d 집계), 시계 오차 의심(ING-06.04, BR-ING-18)
-- -----------------------------------------------------------------------------
ALTER TABLE device_state ADD COLUMN timezone varchar(64);
ALTER TABLE device_state ADD COLUMN clock_skew_since timestamptz;
ALTER TABLE device_state ADD COLUMN clock_skew_suspected boolean NOT NULL DEFAULT false;
COMMENT ON COLUMN device_state.timezone IS '기기가 속한 사이트 시간대(core 기기 정보). 1d 집계 자정 기준(BR-TSD-05). 없으면 조직 기본';
COMMENT ON COLUMN device_state.clock_skew_since IS '1시간 평균 오차가 5분을 처음 넘은 시각(30분 지속되면 이벤트, BR-ING-18)';

-- -----------------------------------------------------------------------------
-- 연장 보관(BR-TSD-02): 측정 항목·모델 보관 기간이 파티션 삭제 기준보다 긴 행을 파티션을 지우기 전에 옮겨 둔다
-- -----------------------------------------------------------------------------
CREATE TABLE telemetry_long (LIKE telemetry INCLUDING DEFAULTS INCLUDING CONSTRAINTS,
    CONSTRAINT pk_telemetry_long PRIMARY KEY (device_id, metric_key, time)
);
CREATE INDEX ix_telemetry_long_org_time ON telemetry_long (organization_id, time);
COMMENT ON TABLE telemetry_long IS '보관 기간이 조직 기본보다 긴 측정 항목의 원본(BR-TSD-02). 파티션 없음, 보관 정리는 행 단위 DELETE';

-- 파티션 정리 기록(BR-TSD-07): 정렬 재작성 시각, 콜드 보관 크기와 비율
ALTER TABLE partition_registries ADD COLUMN sorted_at timestamptz;
ALTER TABLE partition_registries ADD COLUMN archive_bytes bigint;
ALTER TABLE partition_registries ADD COLUMN archive_ratio numeric(8,4);
COMMENT ON COLUMN partition_registries.archive_ratio IS 'Parquet 크기 ÷ DB 원본 크기(TSD-02.03 수용 기준 ≤ 0.2)';

-- -----------------------------------------------------------------------------
-- 콜드 보관 내보내기(TSD-05.02, BR-TSD-18). 파일 목록의 정본은 core archive_files이고, pipeline은 올린 파일과 core 등록 여부만 기록한다
-- (core가 잠시 응답하지 않아도 다음 야간 작업이 등록을 마저 한다. 등록 전에는 원본을 지우지 않는다)
-- -----------------------------------------------------------------------------
CREATE TABLE archive_exports (
    id               bigint GENERATED ALWAYS AS IDENTITY,
    organization_id  bigint        NOT NULL,
    data_class       varchar(24)   NOT NULL,
    range_from       timestamptz   NOT NULL,
    range_to         timestamptz   NOT NULL,
    object_key       varchar(300)  NOT NULL,
    rows_count       bigint        NOT NULL,
    bytes            bigint        NOT NULL,
    source_bytes     bigint,
    checksum         char(64)      NOT NULL,
    registered       boolean       NOT NULL DEFAULT false,
    created_at       timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT pk_archive_exports PRIMARY KEY (id),
    CONSTRAINT ck_archive_exports_range CHECK (range_to > range_from)
);
CREATE UNIQUE INDEX uq_archive_exports_object_key ON archive_exports (object_key);
CREATE INDEX ix_archive_exports_org_class_from ON archive_exports (organization_id, data_class, range_from);

-- -----------------------------------------------------------------------------
-- 스크립트 운영 기록(SCR-03.05·05.01·05.02, SCR domain-model "엔티티(pipeline)"). core-api는 읽기만 한다
-- -----------------------------------------------------------------------------
CREATE TABLE script_errors (
    id               bigint GENERATED ALWAYS AS IDENTITY,
    organization_id  bigint        NOT NULL,
    script_id        bigint        NOT NULL,
    version_id       bigint        NOT NULL,
    version_no       integer       NOT NULL,
    occurred_at      timestamptz   NOT NULL,
    error_code       varchar(32)   NOT NULL,
    message          varchar(500)  NOT NULL,
    line             integer,
    col              integer,
    stack            varchar(2048),
    input_snapshot   jsonb,
    device_id        bigint,
    raw_message_id   bigint,
    CONSTRAINT pk_script_errors PRIMARY KEY (id)
);
CREATE INDEX ix_script_errors_script_occurred ON script_errors (script_id, occurred_at DESC, id DESC);
CREATE INDEX ix_script_errors_org_occurred ON script_errors (organization_id, occurred_at DESC);
COMMENT ON TABLE script_errors IS '스크립트 오류 스냅샷(SCR-05.01). 스크립트별 최근 100건만 유지, 입력 스냅샷 64KB 상한';

CREATE TABLE script_stats_1m (
    script_id        bigint            NOT NULL,
    version_id       bigint            NOT NULL,
    minute           timestamptz       NOT NULL,
    organization_id  bigint            NOT NULL,
    version_no       integer           NOT NULL,
    processed        integer           NOT NULL,
    errors           integer           NOT NULL,
    timeouts         integer           NOT NULL,
    avg_ms           double precision  NOT NULL,
    p95_ms           double precision  NOT NULL,
    max_ms           double precision  NOT NULL,
    max_input_bytes  integer           NOT NULL DEFAULT 0,
    logs_dropped     integer           NOT NULL DEFAULT 0,
    CONSTRAINT pk_script_stats_1m PRIMARY KEY (script_id, version_id, minute)
);
CREATE INDEX ix_script_stats_1m_org_minute ON script_stats_1m (organization_id, minute);
COMMENT ON TABLE script_stats_1m IS '스크립트·버전별 1분 실행 지표(SCR-03.05). 인스턴스끼리 합칠 때 p95·max는 큰 값. 7일 보관 후 1시간 표로';

CREATE TABLE script_stats_1h (LIKE script_stats_1m INCLUDING DEFAULTS,
    CONSTRAINT pk_script_stats_1h PRIMARY KEY (script_id, version_id, minute)
);
CREATE INDEX ix_script_stats_1h_org_minute ON script_stats_1h (organization_id, minute);
COMMENT ON TABLE script_stats_1h IS '스크립트 실행 지표 1시간 단위(90일 보관). minute 열은 시각의 시작';

CREATE TABLE script_logs (
    id               bigint GENERATED ALWAYS AS IDENTITY,
    organization_id  bigint         NOT NULL,
    script_id        bigint         NOT NULL,
    version_no       integer        NOT NULL,
    at               timestamptz    NOT NULL,
    device_id        bigint,
    message          varchar(1100)  NOT NULL,
    CONSTRAINT pk_script_logs PRIMARY KEY (id)
);
CREATE INDEX ix_script_logs_script_at ON script_logs (script_id, at DESC);
COMMENT ON TABLE script_logs IS '운영 중 console.log 수집(SCR-05.02). 수집을 켠 30분 동안 스크립트당 초당 10건, 7일 보관';
