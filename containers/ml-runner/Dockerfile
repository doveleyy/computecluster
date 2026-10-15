FROM python:3.12.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    HOME=/tmp

RUN apt-get update \
    && apt-get install --yes --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt

RUN python -m pip install --no-cache-dir --upgrade pip==25.1.1 \
    && python -m pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cpu \
        torch==2.7.1+cpu \
    && python -m pip install --no-cache-dir -r /tmp/requirements.txt \
    && python -c "import joblib, matplotlib, numpy, pandas, pyarrow, scipy, seaborn, sklearn, statsmodels, torch, xgboost"

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin runner \
    && mkdir -p /workspace/input /workspace/output \
    && chown runner:runner /workspace/output

WORKDIR /workspace
USER 10001:10001

CMD ["python", "-c", "print('Submit a script through Home Platform.')"]
