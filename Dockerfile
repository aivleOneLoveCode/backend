FROM python:3.11-slim

WORKDIR /app

# 시스템 의존성 설치
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Python 의존성 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 애플리케이션 코드 복사
COPY . .

# 포트 노출
EXPOSE 8000

# Gunicorn으로 4개 worker 실행 (로드밸런싱) - 대규모 서비스용
# CMD ["gunicorn", "backend:app", "-w", "4", "-k", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8000"]

# Uvicorn 단일 프로세스 실행 (소규모 서비스용)
CMD ["uvicorn", "backend:app", "--host", "0.0.0.0", "--port", "8000"]