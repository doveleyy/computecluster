# Test-only integration sandbox: the real core and worker as one opaque image.
# Build from the repository root: ops/sandbox/build.sh
FROM docker:28-cli@sha256:625d9431a9f54c5a2bc90f24f0e1c3d55b1349fd857dd85035f98c2c9acbdd4d AS docker-cli

FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f
LABEL org.opencontainers.image.title="Home Platform integration sandbox (test only)" \
      org.opencontainers.image.description="Real core and worker with synthetic accounts. Never attach real data."
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DOCKER_CONFIG=/tmp/docker
RUN python -m pip install --no-cache-dir \
        fastapi==0.141.1 uvicorn==0.52.4 httpx==0.28.1 psutil==7.2.2 \
        python-multipart==0.0.32 pydantic==2.13.5 \
    && groupadd --gid 10001 sandbox \
    && useradd --uid 10001 --gid sandbox --no-create-home --shell /usr/sbin/nologin sandbox \
    && mkdir -p /sandbox/state /sandbox/credentials /sandbox/app /sandbox/app-state \
    && touch /sandbox/manifest.json \
    && chown -R sandbox:sandbox /sandbox
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /app
COPY app app
COPY contracts contracts
COPY worker worker
COPY ops ops
COPY ops/sandbox/compose.yaml /sandbox/compose.yaml
USER 10001:10001
EXPOSE 8000 8001
CMD ["python", "-m", "ops.sandbox"]
