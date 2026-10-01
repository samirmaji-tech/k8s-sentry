# =====================================================================
# K8s-Sentry — container image for the autonomous AI SRE agent
# Multi-stage build: wheels are compiled in a builder, runtime stays slim.
# =====================================================================

# ---------- Stage 1: builder ----------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

COPY requirements.txt .

RUN pip install --prefix=/install -r requirements.txt

# ---------- Stage 2: runtime ----------
FROM python:3.12-slim AS runtime

# Create an unprivileged user. The agent never needs root.
RUN groupadd --gid 10001 sentry \
    && useradd --uid 10001 --gid sentry --create-home --shell /usr/sbin/nologin sentry

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/install/bin:$PATH" \
    PYTHONPATH="/install/lib/python3.12/site-packages"

WORKDIR /app

# Bring in the pre-built dependencies from the builder stage.
COPY --from=builder /install /install

# Application code.
COPY app/ ./app/

USER 10001

EXPOSE 8080

# Basic container-level healthcheck against the readiness endpoint.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
    sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:8080/healthz').status==200 else sys.exit(1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
