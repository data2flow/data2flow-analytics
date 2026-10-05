-- =============================================================================
-- data2flow_pipeline: ingress payload 형식 변환·토픽 템플릿 결과 보관(DSC-09.07·09.08, BR-DSC-28, ADR-056)
-- ADR-030: staging 배포 때만 migrate, prod는 validate. 확장(추가)만 한다 — 모두 NULL 허용, 기존 열·제약은 바꾸지 않는다.
-- =============================================================================

-- ingress가 payload를 구조화된 JSON으로 바꿨을 때 원래 형식과 받은 그대로의 바이트(무손실 보관). payload 열은 바꾼 JSON이다
ALTER TABLE raw_messages ADD COLUMN payload_format varchar(16);
ALTER TABLE raw_messages ADD COLUMN original_payload bytea;
-- 토픽 템플릿으로 뽑은 값(externalId·metric·spaceHint와 변수들). 재처리에서도 같은 값으로 기기·측정 항목을 정한다
ALTER TABLE raw_messages ADD COLUMN topic_attributes jsonb;
ALTER TABLE raw_messages ADD CONSTRAINT ck_raw_messages_payload_format
    CHECK (payload_format IS NULL OR payload_format IN ('JSON','CBOR','MSGPACK','PROTOBUF','AVRO','CSV','TEXT','BINARY','SPARKPLUG_B'));
ALTER TABLE raw_messages ADD CONSTRAINT ck_raw_messages_original_payload_size
    CHECK (original_payload IS NULL OR octet_length(original_payload) <= 262144);
COMMENT ON COLUMN raw_messages.payload_format IS 'ingress가 변환한 원래 형식(RawEnvelope.payloadFormat, DSC-09.07)';
COMMENT ON COLUMN raw_messages.original_payload IS '변환 전 받은 그대로의 바이트(RawEnvelope.originalPayload). 256KB 초과는 앞 4KB만';
COMMENT ON COLUMN raw_messages.topic_attributes IS '토픽 템플릿 추출 값(RawEnvelope.topicAttributes, DSC-09.08)';
