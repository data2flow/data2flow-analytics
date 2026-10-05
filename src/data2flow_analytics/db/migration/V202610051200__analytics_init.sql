-- data2flow_analytics 초기 스키마 (Flyway 형식 V1, 소유: data2flow-analytics)
-- 정본 DDL 초안 design/erd/ddl/30-analytics.sql을 그대로 옮기고, 작업 큐 jobs에 작업 매개변수 payload(jsonb)를 더했다
-- 근거: design/erd/analytics.md, spec/detail/ANA/domain-model.md, spec/detail/ENE/domain-model.md §3
-- 규칙: design/erd/README.md (테이블 복수형, BIGINT IDENTITY, 같은 스키마 안에서만 FK, varchar+CHECK)

CREATE SCHEMA IF NOT EXISTS data2flow_analytics;

-- ───────────── Template ─────────────
CREATE TABLE data2flow_analytics.templates (
  id               bigint GENERATED ALWAYS AS IDENTITY,
  key              varchar(64)  NOT NULL,
  version          varchar(16)  NOT NULL,
  name             varchar(100) NOT NULL,
  kind             varchar(16)  NOT NULL,
  category         varchar(32)  NOT NULL,
  roles            jsonb        NOT NULL,
  params_schema    jsonb        NOT NULL,
  requirements     jsonb        NOT NULL,
  guide_md         text         NOT NULL,
  fast             boolean      NOT NULL DEFAULT false,
  realtime         boolean      NOT NULL DEFAULT false,
  trainable        boolean      NOT NULL DEFAULT false,
  sample_image_url varchar(500),
  current          boolean      NOT NULL DEFAULT false,
  created_at       timestamptz  NOT NULL DEFAULT now(),
  updated_at       timestamptz  NOT NULL DEFAULT now(),
  CONSTRAINT pk_templates PRIMARY KEY (id),
  CONSTRAINT uq_templates_key_version UNIQUE (key, version),
  CONSTRAINT ck_templates_kind CHECK (kind IN ('GENERAL','DOMAIN')),
  CONSTRAINT ck_templates_category CHECK (category IN ('ENV_QUALITY','SPACE_USAGE','ASSET_HEALTH','PREDICTION','GENERAL'))
);
CREATE UNIQUE INDEX uq_templates_key_current ON data2flow_analytics.templates (key) WHERE current;
COMMENT ON TABLE data2flow_analytics.templates IS '분석 템플릿(전역, 코드에서 자동 등록). BR-ANA-01';

CREATE TABLE data2flow_analytics.template_settings (
  id              bigint GENERATED ALWAYS AS IDENTITY,
  organization_id bigint      NOT NULL,
  template_key    varchar(64) NOT NULL,
  enabled         boolean     NOT NULL DEFAULT true,
  version         integer     NOT NULL DEFAULT 0,
  created_by      bigint,
  updated_by      bigint,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_template_settings PRIMARY KEY (id),
  CONSTRAINT uq_template_settings_organization_id_template_key UNIQUE (organization_id, template_key)
);
COMMENT ON TABLE data2flow_analytics.template_settings IS '조직별 템플릿 활성화. ANA-01.03, BR-ANA-17';

-- ───────────── Analysis ─────────────
CREATE TABLE data2flow_analytics.datasets (
  id              bigint GENERATED ALWAYS AS IDENTITY,
  organization_id bigint       NOT NULL,
  name            varchar(100) NOT NULL,
  bindings        jsonb        NOT NULL,
  period          jsonb        NOT NULL,
  quality_filter  varchar(16)  NOT NULL DEFAULT 'NORMAL_ONLY',
  include_virtual boolean      NOT NULL DEFAULT false,
  version         integer      NOT NULL DEFAULT 1,
  created_by      bigint,
  updated_by      bigint,
  created_at      timestamptz  NOT NULL DEFAULT now(),
  updated_at      timestamptz  NOT NULL DEFAULT now(),
  CONSTRAINT pk_datasets PRIMARY KEY (id),
  CONSTRAINT ck_datasets_quality_filter CHECK (quality_filter IN ('NORMAL_ONLY','INCLUDE_ALL'))
);
CREATE INDEX ix_datasets_organization_id ON data2flow_analytics.datasets (organization_id, id);
COMMENT ON COLUMN data2flow_analytics.datasets.version IS '바뀔 때마다 증가(BR-ANA-21). 낙관적 잠금도 이 값을 쓴다';

CREATE TABLE data2flow_analytics.dataset_versions (
  id              bigint GENERATED ALWAYS AS IDENTITY,
  organization_id bigint      NOT NULL,
  dataset_id      bigint      NOT NULL,
  version         integer     NOT NULL,
  bindings        jsonb       NOT NULL,
  period          jsonb       NOT NULL,
  quality_filter  varchar(16) NOT NULL,
  include_virtual boolean     NOT NULL,
  created_by      bigint,
  created_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_dataset_versions PRIMARY KEY (id),
  CONSTRAINT fk_dataset_versions_dataset_id FOREIGN KEY (dataset_id) REFERENCES data2flow_analytics.datasets (id) ON DELETE CASCADE,
  CONSTRAINT uq_dataset_versions_dataset_id_version UNIQUE (dataset_id, version),
  CONSTRAINT ck_dataset_versions_quality_filter CHECK (quality_filter IN ('NORMAL_ONLY','INCLUDE_ALL'))
);
COMMENT ON TABLE data2flow_analytics.dataset_versions IS '데이터셋 버전 이력(ANA-10.01). 만들거나 고칠 때 같은 트랜잭션에서 한 행씩';

CREATE TABLE data2flow_analytics.analyses (
  id                   bigint GENERATED ALWAYS AS IDENTITY,
  organization_id      bigint       NOT NULL,
  template_key         varchar(64)  NOT NULL,
  template_version     varchar(16)  NOT NULL,
  dataset_id           bigint,
  dataset_version      integer,
  name                 varchar(100) NOT NULL,
  bindings             jsonb        NOT NULL,
  period               jsonb        NOT NULL,
  resolution           varchar(8)   NOT NULL DEFAULT 'AUTO',
  quality_filter       varchar(16)  NOT NULL DEFAULT 'NORMAL_ONLY',
  include_virtual      boolean      NOT NULL DEFAULT false,
  params               jsonb        NOT NULL DEFAULT '{}'::jsonb,
  schedule             jsonb,
  schedule_state       varchar(24),
  consecutive_failures smallint     NOT NULL DEFAULT 0,
  recipients           jsonb,
  retrain_policy       varchar(16),
  realtime             boolean      NOT NULL DEFAULT false,
  owner_user_id        bigint       NOT NULL,
  space_scope_ids      bigint[]     NOT NULL DEFAULT '{}',
  status               varchar(16)  NOT NULL DEFAULT 'ACTIVE',
  version              integer      NOT NULL DEFAULT 0,
  created_by           bigint,
  updated_by           bigint,
  created_at           timestamptz  NOT NULL DEFAULT now(),
  updated_at           timestamptz  NOT NULL DEFAULT now(),
  CONSTRAINT ck_analyses_dataset_version CHECK ((dataset_id IS NULL) = (dataset_version IS NULL)),
  CONSTRAINT pk_analyses PRIMARY KEY (id),
  CONSTRAINT fk_analyses_template_key_template_version FOREIGN KEY (template_key, template_version)
    REFERENCES data2flow_analytics.templates (key, version) ON DELETE RESTRICT,
  CONSTRAINT fk_analyses_dataset_id FOREIGN KEY (dataset_id) REFERENCES data2flow_analytics.datasets (id) ON DELETE RESTRICT,
  CONSTRAINT ck_analyses_resolution CHECK (resolution IN ('AUTO','RAW','1m','1h','1d')),
  CONSTRAINT ck_analyses_quality_filter CHECK (quality_filter IN ('NORMAL_ONLY','INCLUDE_ALL')),
  CONSTRAINT ck_analyses_schedule_state CHECK (schedule_state IS NULL OR schedule_state IN ('ACTIVE','PAUSED','STOPPED_BY_FAILURE')),
  CONSTRAINT ck_analyses_retrain_policy CHECK (retrain_policy IS NULL OR retrain_policy IN ('MANUAL','SCHEDULE','ON_DRIFT')),
  CONSTRAINT ck_analyses_status CHECK (status IN ('ACTIVE','ARCHIVED'))
);
CREATE INDEX ix_analyses_organization_id_owner_user_id ON data2flow_analytics.analyses (organization_id, owner_user_id);
CREATE INDEX ix_analyses_organization_id_schedule_state_active ON data2flow_analytics.analyses (organization_id) WHERE schedule_state = 'ACTIVE';
CREATE INDEX ix_analyses_space_scope_ids ON data2flow_analytics.analyses USING gin (space_scope_ids);
COMMENT ON COLUMN data2flow_analytics.analyses.space_scope_ids IS '바인딩이 참조하는 공간(권한 필터, 저장 시 계산). BR-ANA-03';
COMMENT ON COLUMN data2flow_analytics.analyses.status IS '도메인 문서의 deleted_at(소프트 삭제) 대신 상태로 표현(erd/README §5)';

CREATE TABLE data2flow_analytics.runs (
  id               bigint GENERATED ALWAYS AS IDENTITY,
  organization_id  bigint      NOT NULL,
  analysis_id      bigint      NOT NULL,
  template_version varchar(16) NOT NULL,
  trigger          varchar(16) NOT NULL,
  status           varchar(16) NOT NULL DEFAULT 'PENDING',
  queue_position   integer,
  progress         smallint,
  stage            varchar(32),
  period_from      timestamptz NOT NULL,
  period_to        timestamptz NOT NULL,
  provenance       jsonb,
  error_code       varchar(64),
  error_message    text,
  error_detail     text,
  requested_by     bigint,
  started_at       timestamptz,
  finished_at      timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_runs PRIMARY KEY (id),
  CONSTRAINT fk_runs_analysis_id FOREIGN KEY (analysis_id) REFERENCES data2flow_analytics.analyses (id) ON DELETE CASCADE,
  CONSTRAINT ck_runs_trigger CHECK (trigger IN ('MANUAL','SCHEDULE','API')),
  CONSTRAINT ck_runs_status CHECK (status IN ('QUEUED','PENDING','RUNNING','SUCCEEDED','FAILED','TIMEOUT','CANCELLED')),
  CONSTRAINT ck_runs_progress CHECK (progress IS NULL OR progress BETWEEN 0 AND 100),
  CONSTRAINT ck_runs_stage CHECK (stage IS NULL OR stage IN ('LOAD','COMPUTE','SAVE')),
  CONSTRAINT ck_runs_period CHECK (period_from < period_to)
);
CREATE INDEX ix_runs_analysis_id_created_at ON data2flow_analytics.runs (analysis_id, created_at DESC);
CREATE INDEX ix_runs_organization_id_status_active ON data2flow_analytics.runs (organization_id, status) WHERE status IN ('QUEUED','PENDING','RUNNING');
COMMENT ON COLUMN data2flow_analytics.runs.provenance IS '포인트 수, 누락률, 품질 필터, 가상 포함, 데이터셋 버전, 시드(BR-ANA-10)';

CREATE TABLE data2flow_analytics.results (
  run_id          bigint      NOT NULL,
  organization_id bigint      NOT NULL,
  summary         jsonb       NOT NULL,
  charts          jsonb       NOT NULL DEFAULT '[]'::jsonb,
  tables          jsonb       NOT NULL DEFAULT '[]'::jsonb,
  evidence        jsonb,
  caveats         text[],
  ai_commentary   jsonb,
  content_hash    char(64),
  expires_at      timestamptz NOT NULL DEFAULT (now() + interval '1 year'),
  created_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_results PRIMARY KEY (run_id),
  CONSTRAINT fk_results_run_id FOREIGN KEY (run_id) REFERENCES data2flow_analytics.runs (id) ON DELETE CASCADE
);
CREATE INDEX ix_results_expires_at ON data2flow_analytics.results (expires_at);
COMMENT ON COLUMN data2flow_analytics.results.content_hash IS '결과 본문 SHA-256(재현성 비교, ANA-10.02 테스트 근거). 문서에 컬럼 정의 없음 — 확인 필요';

CREATE TABLE data2flow_analytics.jobs (
  id              bigint GENERATED ALWAYS AS IDENTITY,
  organization_id bigint      NOT NULL,
  type            varchar(16) NOT NULL,
  ref_id          bigint      NOT NULL,
  status          varchar(16) NOT NULL DEFAULT 'READY',
  locked_by       varchar(64),
  locked_until    timestamptz,
  attempts        smallint    NOT NULL DEFAULT 0,
  available_at    timestamptz NOT NULL DEFAULT now(),
  last_error      text,
  payload         jsonb       NOT NULL DEFAULT '{}'::jsonb,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_jobs PRIMARY KEY (id),
  CONSTRAINT ck_jobs_type CHECK (type IN ('RUN','TRAIN','EXPORT')),
  CONSTRAINT ck_jobs_status CHECK (status IN ('READY','LOCKED','DONE','DEAD')),
  CONSTRAINT ck_jobs_attempts CHECK (attempts BETWEEN 0 AND 3)
);
CREATE INDEX ix_jobs_available_at_ready ON data2flow_analytics.jobs (available_at) WHERE status = 'READY';
CREATE INDEX ix_jobs_locked_until_locked ON data2flow_analytics.jobs (locked_until) WHERE status = 'LOCKED';
COMMENT ON TABLE data2flow_analytics.jobs IS '작업 큐. SELECT … FOR UPDATE SKIP LOCKED, 3회 실패 DEAD(erd/README §11.3)';

CREATE TABLE data2flow_analytics.model_artifacts (
  id               bigint GENERATED ALWAYS AS IDENTITY,
  organization_id  bigint      NOT NULL,
  analysis_id      bigint      NOT NULL,
  template_key     varchar(64) NOT NULL,
  template_version varchar(16) NOT NULL,
  binding_hash     char(64)    NOT NULL,
  artifact_version integer     NOT NULL,
  status           varchar(16) NOT NULL DEFAULT 'TRAINING',
  uri              varchar(500),
  train_from       timestamptz NOT NULL,
  train_to         timestamptz NOT NULL,
  params           jsonb       NOT NULL DEFAULT '{}'::jsonb,
  metrics          jsonb,
  trained_at       timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_model_artifacts PRIMARY KEY (id),
  CONSTRAINT fk_model_artifacts_analysis_id FOREIGN KEY (analysis_id) REFERENCES data2flow_analytics.analyses (id) ON DELETE CASCADE,
  CONSTRAINT uq_model_artifacts_analysis_id_artifact_version UNIQUE (analysis_id, artifact_version),
  CONSTRAINT ck_model_artifacts_status CHECK (status IN ('TRAINING','CANDIDATE','ACTIVE','RETIRED','FAILED'))
);
CREATE UNIQUE INDEX uq_model_artifacts_analysis_id_active ON data2flow_analytics.model_artifacts (analysis_id) WHERE status = 'ACTIVE';
COMMENT ON COLUMN data2flow_analytics.model_artifacts.artifact_version IS '도메인 문서의 version(1부터 증가). 낙관적 잠금 컬럼 version과 겹치지 않게 이름을 바꿈';

CREATE TABLE data2flow_analytics.realtime_events (
  id              bigint GENERATED ALWAYS AS IDENTITY,
  organization_id bigint      NOT NULL,
  analysis_id     bigint      NOT NULL,
  type            varchar(16) NOT NULL,
  device_id       bigint      NOT NULL,
  metric_key      varchar(64) NOT NULL,
  occurred_at     timestamptz NOT NULL,
  payload         jsonb       NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_realtime_events PRIMARY KEY (id, occurred_at),
  CONSTRAINT fk_realtime_events_analysis_id FOREIGN KEY (analysis_id) REFERENCES data2flow_analytics.analyses (id) ON DELETE CASCADE,
  CONSTRAINT ck_realtime_events_type CHECK (type IN ('ANOMALY','ETA'))
) PARTITION BY RANGE (occurred_at);
CREATE TABLE data2flow_analytics.realtime_events_default PARTITION OF data2flow_analytics.realtime_events DEFAULT;
CREATE TABLE data2flow_analytics.realtime_events_y2026m10 PARTITION OF data2flow_analytics.realtime_events
  FOR VALUES FROM ('2026-10-01 00:00:00+00') TO ('2026-11-01 00:00:00+00');
CREATE INDEX ix_realtime_events_organization_id_device_id_occurred_at ON data2flow_analytics.realtime_events (organization_id, device_id, occurred_at DESC);
COMMENT ON COLUMN data2flow_analytics.realtime_events.device_id IS 'data2flow_core.devices.id (FK 없음)';

CREATE TABLE data2flow_analytics.anomaly_feedback (
  id                bigint GENERATED ALWAYS AS IDENTITY,
  organization_id   bigint       NOT NULL,
  run_id            bigint,
  realtime_event_id bigint,
  occurred_at       timestamptz  NOT NULL,
  series_key        varchar(200) NOT NULL,
  verdict           varchar(16)  NOT NULL,
  user_id           bigint       NOT NULL,
  created_at        timestamptz  NOT NULL DEFAULT now(),
  CONSTRAINT pk_anomaly_feedback PRIMARY KEY (id),
  CONSTRAINT fk_anomaly_feedback_run_id FOREIGN KEY (run_id) REFERENCES data2flow_analytics.runs (id) ON DELETE CASCADE,
  CONSTRAINT ck_anomaly_feedback_verdict CHECK (verdict IN ('TRUE_POSITIVE','FALSE_POSITIVE')),
  CONSTRAINT ck_anomaly_feedback_source CHECK ((run_id IS NULL) <> (realtime_event_id IS NULL))
);
CREATE INDEX ix_anomaly_feedback_organization_id_run_id ON data2flow_analytics.anomaly_feedback (organization_id, run_id);
COMMENT ON COLUMN data2flow_analytics.anomaly_feedback.realtime_event_id IS 'realtime_events.id. 파티션 테이블이라 FK 없음(파티션 키 occurred_at 포함 필요)';

-- ───────────── EnergyComputation (ENE 계산) ─────────────
CREATE TABLE data2flow_analytics.energy_daily (
  id                bigint GENERATED ALWAYS AS IDENTITY,
  organization_id   bigint           NOT NULL,
  space_id          bigint           NOT NULL,
  day               date             NOT NULL,
  source_kind       varchar(16)      NOT NULL,
  kwh               double precision NOT NULL,
  krw               numeric(14,2),
  tco2              double precision,
  occupied_hours    double precision,
  includes_estimate boolean          NOT NULL DEFAULT false,
  computed_at       timestamptz      NOT NULL DEFAULT now(),
  created_at        timestamptz      NOT NULL DEFAULT now(),
  updated_at        timestamptz      NOT NULL DEFAULT now(),
  CONSTRAINT pk_energy_daily PRIMARY KEY (id),
  CONSTRAINT uq_energy_daily_organization_id_space_id_day_source_kind UNIQUE (organization_id, space_id, day, source_kind),
  CONSTRAINT ck_energy_daily_source_kind CHECK (source_kind IN ('METERED','ESTIMATED','BILL_ALLOCATED'))
);
COMMENT ON TABLE data2flow_analytics.energy_daily IS '공간 × 일 × 출처 에너지 집계. 매일 02:00 확정, 최근 7일 재계산(ENE §3)';

CREATE TABLE data2flow_analytics.baseline_models (
  id              bigint GENERATED ALWAYS AS IDENTITY,
  organization_id bigint           NOT NULL,
  key             varchar(64)      NOT NULL,
  scope           jsonb            NOT NULL,
  method          varchar(32)      NOT NULL,
  coefficients    jsonb            NOT NULL,
  r2              double precision,
  residuals       jsonb,
  trained_at      timestamptz      NOT NULL,
  created_at      timestamptz      NOT NULL DEFAULT now(),
  CONSTRAINT pk_baseline_models PRIMARY KEY (id),
  CONSTRAINT ck_baseline_models_method CHECK (method IN ('PERIOD_AVERAGE','DEGREE_DAY_REGRESSION')),
  CONSTRAINT uq_baseline_models_organization_id_key UNIQUE (organization_id, key)
);
COMMENT ON COLUMN data2flow_analytics.baseline_models.key IS 'data2flow_core.energy_baseline_versions.analytics_model_id가 가리키는 키';
COMMENT ON COLUMN data2flow_analytics.baseline_models.method IS 'ENE energy_baseline_versions.method와 같은 값(2026-10-03)';

CREATE TABLE data2flow_analytics.ach_estimates (
  id              bigint GENERATED ALWAYS AS IDENTITY,
  organization_id bigint           NOT NULL,
  space_id        bigint           NOT NULL,
  day             date             NOT NULL,
  ach             double precision NOT NULL,
  ci_low          double precision,
  ci_high         double precision,
  decay_from      timestamptz,
  decay_to        timestamptz,
  r2_fit          double precision,
  created_at      timestamptz      NOT NULL DEFAULT now(),
  CONSTRAINT pk_ach_estimates PRIMARY KEY (id),
  CONSTRAINT uq_ach_estimates_organization_id_space_id_day UNIQUE (organization_id, space_id, day)
);

CREATE TABLE data2flow_analytics.opportunity_detections (
  id              bigint GENERATED ALWAYS AS IDENTITY,
  organization_id bigint           NOT NULL,
  run_id          bigint,
  type            varchar(32)      NOT NULL,
  space_id        bigint,
  device_id       bigint,
  window_from     timestamptz      NOT NULL,
  window_to       timestamptz      NOT NULL,
  evidence        jsonb            NOT NULL,
  est_kwh         double precision,
  detected_at     timestamptz      NOT NULL DEFAULT now(),
  created_at      timestamptz      NOT NULL DEFAULT now(),
  CONSTRAINT pk_opportunity_detections PRIMARY KEY (id)
);
CREATE INDEX ix_opportunity_detections_organization_id_detected_at ON data2flow_analytics.opportunity_detections (organization_id, detected_at);
COMMENT ON TABLE data2flow_analytics.opportunity_detections IS '원시 탐지. 이벤트 발행 후 30일 보관(ENE §3)';
COMMENT ON COLUMN data2flow_analytics.opportunity_detections.run_id IS 'ENE 계산 실행 식별자. 무엇을 가리키는지 문서에 없음 — 확인 필요';

CREATE TABLE data2flow_analytics.fdd_evaluations (
  id              bigint GENERATED ALWAYS AS IDENTITY,
  organization_id bigint      NOT NULL,
  run_id          bigint,
  rule_key        varchar(64) NOT NULL,
  device_id       bigint      NOT NULL,
  window_from     timestamptz NOT NULL,
  window_to       timestamptz NOT NULL,
  passed          boolean     NOT NULL,
  evidence        jsonb,
  evaluated_at    timestamptz NOT NULL DEFAULT now(),
  created_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_fdd_evaluations PRIMARY KEY (id)
);
CREATE INDEX ix_fdd_evaluations_organization_id_device_id_evaluated_at ON data2flow_analytics.fdd_evaluations (organization_id, device_id, evaluated_at DESC);

CREATE TABLE data2flow_analytics.mv_runs (
  id              bigint GENERATED ALWAYS AS IDENTITY,
  organization_id bigint      NOT NULL,
  opportunity_id  bigint      NOT NULL,
  inputs          jsonb       NOT NULL,
  outputs         jsonb,
  created_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_mv_runs PRIMARY KEY (id)
);
CREATE INDEX ix_mv_runs_organization_id_opportunity_id ON data2flow_analytics.mv_runs (organization_id, opportunity_id);
COMMENT ON COLUMN data2flow_analytics.mv_runs.opportunity_id IS 'data2flow_core.energy_opportunities.id (FK 없음). M&V 재현용 입력 스냅샷';

COMMENT ON COLUMN data2flow_analytics.jobs.payload IS '작업 매개변수(EXPORT: format·chartId·tableId·requestedBy, TRAIN: trainFrom·trainTo·params)';
