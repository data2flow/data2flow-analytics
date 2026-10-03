# data2flow-analytics

분석 서비스입니다. 분석 템플릿 카탈로그, 데이터 충분성 확인, 분석 실행·학습, 실시간 추론을 맡습니다(Python FastAPI, ADR-010·011).

- 관련 스펙: ANA (정본은 비공개 저장소 `data2flow-docs`)
- 포트: API 8080, 관리 8081(`/actuator/health/liveness`, `/actuator/health/readiness`, `/actuator/prometheus`)

## 개발

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest                       # 테스트 + 커버리지 80%
python -m data2flow_analytics
```
