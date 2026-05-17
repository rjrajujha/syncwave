FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-*}\" --ws-ping-interval ${WS_PING_INTERVAL:-20} --ws-ping-timeout ${WS_PING_TIMEOUT:-20} --timeout-keep-alive ${KEEP_ALIVE_TIMEOUT:-10}"]
