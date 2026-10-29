-- ENE-01.04 기준선 학습(API-ENE-81)은 작업 큐의 BASELINE 작업으로 돈다. ref_id = data2flow_core.energy_baseline_versions.id
ALTER TABLE data2flow_analytics.jobs DROP CONSTRAINT ck_jobs_type;
ALTER TABLE data2flow_analytics.jobs ADD CONSTRAINT ck_jobs_type CHECK (type IN ('RUN','TRAIN','EXPORT','BASELINE'));
