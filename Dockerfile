# data2flow-analytics 실행 이미지. 비루트(UID 10001), TZ=UTC, API 8080·관리 8081
# 같은 이미지로 analytics-api(기본 CMD api)와 analytics-worker(args: worker)를 띄운다(design/analytics-service.md §2)
FROM python:3.12-slim
# 베이스 이미지의 OS 패키지 보안 업데이트를 반영한다(Trivy HIGH 이상 0 유지)
RUN apt-get update && apt-get -y upgrade && rm -rf /var/lib/apt/lists/*
# matplotlib 설정 캐시는 쓰기 가능한 /tmp(emptyDir)에 둔다(readOnlyRootFilesystem)
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=UTC PIP_NO_CACHE_DIR=1 MPLCONFIGDIR=/tmp/matplotlib \
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --no-create-home app
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-compile .
USER 10001
EXPOSE 8080 8081
ENTRYPOINT ["python", "-m", "data2flow_analytics"]
CMD ["api"]
