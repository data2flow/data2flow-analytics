# data2flow-analytics

분석 서비스입니다. 분석 템플릿 카탈로그, 데이터 충분성 확인, 분석 실행·학습, 실시간 추론을 맡습니다(Python FastAPI, ADR-010·011·058).

- 관련 스펙: ANA (정본은 비공개 저장소 `data2flow-docs`: `spec/ANA-analytics.md`, `design/analytics-service.md`, `design/api/ANA-api.md`)
- 포트: API 8080(`/internal/analytics/**`, core-api만 호출), 관리 8081(`/actuator/health/liveness`, `/actuator/health/readiness`, `/actuator/prometheus`, `/health`, `/metrics`)
- 같은 이미지로 두 가지를 띄웁니다: `python -m data2flow_analytics api`(기본) / `python -m data2flow_analytics worker`(작업 큐·정리·실시간 추론)

## 무엇이 들어 있나

| 영역 | 위치 |
|---|---|
| 템플릿 13종(폴더 하나 = 템플릿 하나, `template.py` + `GUIDE.md`) | `src/data2flow_analytics/templates/builtin/` |
| 템플릿 계약(역할·요구 조건·결과·ChartSpec) | `templates/base.py`, `schemas/*.json` |
| 바인딩 → SQL 로더(`data2flow_pipeline` 시계열 읽기 전용), 충분성·한도 | `loader/` |
| 분석 정의·실행 요청·대기열(조직당 5건)·데이터셋 | `analysis/` |
| 작업 큐(`FOR UPDATE SKIP LOCKED`, 3회 실패 DEAD)·실행기(시간 제한·취소) | `worker/` |
| 실시간 추론(Super Stream `data2flow.telemetry`, 그룹 `analytics`) | `realtime/` |
| 모델 학습·버전·적용·드리프트(PSI) | `models/` |
| 내보내기(CSV·PNG·PDF), KPI | `export/`, `kpi.py` |
| Flyway 형식 마이그레이션 | `db/migration/`, `db/migrate.py` |

## 개발

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check src tests          # 린트
pytest                        # 단위·계약·통합(Testcontainers PostgreSQL 18·RabbitMQ 4) + 커버리지 80%
pytest -k contract            # 템플릿 계약만
pytest -m accuracy            # 야간 정확도(시드 30~50개), accuracy-report.json
UPDATE_GOLDEN=1 pytest tests/regression   # 골든 결과 다시 만들기(의존성 업그레이드 때 검토 후)
```

통합 테스트는 Docker가 필요합니다. s3·s4 공용 서버에는 닿지 않습니다.

로컬 실행(공용 개발 DB, 실시간은 기본으로 꺼짐 — 운영과 같은 장비 데이터를 두 번 처리하지 않도록):

```bash
set -a; source ../.env; set +a        # DATA2FLOW_DB_* 등
DATA2FLOW_DEV_NAME=<이름> DATA2FLOW_CORE_URI=http://localhost:8082 python -m data2flow_analytics api
```

주요 환경변수: `DATA2FLOW_PROFILE`(local·staging·prod — staging만 마이그레이션 적용, 나머지는 검증), `DATA2FLOW_DB_*`,
`DATA2FLOW_RABBITMQ_*`(AMQP·Stream 포트, vhost), `DATA2FLOW_CORE_URI`, `DATA2FLOW_ARCHIVE_*`(모델·내보내기 S3 호환 저장소, 없으면 `/tmp`),
`DATA2FLOW_ANALYTICS_REALTIME_ENABLED`, `DATA2FLOW_ANALYTICS_WORKER_THREADS`, `DATA2FLOW_ANALYTICS_TEMPLATE_DIRS`(추가 템플릿 폴더).

## 템플릿 추가

1. `templates/builtin/{key}/template.py`에 `Template`을 상속한 클래스와 `TEMPLATE = …`, 같은 폴더에 `GUIDE.md`(9항목: 한 줄 요약,
   언제 쓰나 3개 이상, 언제 쓰면 안 되나, 필요한 데이터(최소 기간·포인트·누락률 수치), 파라미터(모든 필드), 결과 읽는 법, 주의점, 사용 예시,
   알고리즘·참고 문헌)를 둡니다. 설명서가 빠지면 등록되지 않고 시작 로그에 `template=… missing=[…]`가 남습니다.
2. `pytest -k contract`로 설명서·스키마·결과 형식·결정성을 확인합니다. 결과 차트는 ChartSpec 9종만 쓰면 화면 변경이 필요 없습니다.
