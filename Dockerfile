FROM python:3.11-slim

WORKDIR /app

# 시스템 의존성
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ && \
    rm -rf /var/lib/apt/lists/*

# Python 의존성
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 프로젝트 코드
COPY . .

# 환경 변수 (실행 시 .env 마운트)
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# 기본 실행: E2E 파이프라인
ENTRYPOINT ["python"]
CMD ["jobs/run_e2e_pipeline.py"]
