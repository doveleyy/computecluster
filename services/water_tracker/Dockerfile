FROM python:3.13.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/home-platform

COPY services/water_tracker/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --requirement /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY services/__init__.py services/__init__.py
COPY services/water_tracker services/water_tracker

USER 10001:10001
EXPOSE 8100

CMD ["uvicorn", "services.water_tracker.main:app", "--host", "0.0.0.0", "--port", "8100", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]
