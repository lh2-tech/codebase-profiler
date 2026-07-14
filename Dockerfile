# syntax=docker/dockerfile:1

FROM python:3.11-slim-bookworm

ARG TARGETARCH
ARG SCC_VERSION=3.7.0

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl tar \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    case "${TARGETARCH}" in \
      arm64) SCC_ARCH=arm64 ;; \
      amd64) SCC_ARCH=x86_64 ;; \
      *) SCC_ARCH=x86_64 ;; \
    esac; \
    curl -fsSL "https://github.com/boyter/scc/releases/download/v${SCC_VERSION}/scc_Linux_${SCC_ARCH}.tar.gz" \
        -o /tmp/scc.tgz; \
    tar -xzf /tmp/scc.tgz -C /usr/local/bin scc; \
    rm /tmp/scc.tgz; \
    scc --version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY extract_org_raw_data.py extract_ui.py count_merged_prs.py github_app_auth.py ./
COPY tokens.example .

RUN mkdir -p /app/outputs/raw-extracts /data/repos

ENV EXTRACT_UI_DOCKER=1 \
    DEFAULT_LOCAL_REPOS_DIR=/data/repos \
    DEFAULT_TOKENS_FILE=/app/tokens \
    HOST_OUTPUT_HINT=./outputs/raw-extracts \
    PYTHONUNBUFFERED=1

EXPOSE 8766

CMD ["python", "extract_org_raw_data.py", "--ui", "--ui-host", "0.0.0.0", "--ui-port", "8766"]
