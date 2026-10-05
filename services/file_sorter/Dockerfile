FROM python:3.13.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/home-platform

COPY services/file_sorter/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --requirement /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY services/__init__.py services/__init__.py
COPY services/common services/common
COPY services/file_sorter services/file_sorter

USER 10001:10001
EXPOSE 8103

CMD ["uvicorn", "services.file_sorter.main:app", "--host", "0.0.0.0", "--port", "8103", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]
