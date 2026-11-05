FROM python:3.11-slim-bookworm

ARG TARGETARCH=amd64
ARG SCC_VERSION=3.7.0
ARG P4_VERSION=r25.2

RUN apt-get update && apt-get install -y --no-install-recommends \
        git mercurial ca-certificates curl tar \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    case "${TARGETARCH}" in \
      arm64) SCC_ARCH=arm64 ;; \
      amd64) SCC_ARCH=x86_64 ;; \
      *) SCC_ARCH=x86_64 ;; \
    esac; \
    #added a retry loop for curl for SSL issues 
    for attempt in 1 2 3; do \
      curl -fsSL "https://github.com/boyter/scc/releases/download/v${SCC_VERSION}/scc_Linux_${SCC_ARCH}.tar.gz" \
        -o /tmp/scc.tgz && break || ([ $attempt -eq 3 ] && exit 1); \
      sleep 2; \
    done; \
    tar -xzf /tmp/scc.tgz -C /usr/local/bin scc; \
    rm /tmp/scc.tgz; \
    scc --version

# Perforce command-line client (not in Debian's repositories); used by the "Perforce (server)" mode.
# The arch comes from the running image (uname), not TARGETARCH: that ARG is only set by buildx, and an
# x86_64 p4 in an arm64 image fails to start (it is dynamically linked).
RUN set -eux; \
    case "$(uname -m)" in \
      aarch64|arm64) P4_ARCH=linux26aarch64 ;; \
      *) P4_ARCH=linux26x86_64 ;; \
    esac; \
    for attempt in 1 2 3; do \
      curl -fsSL "https://cdist2.perforce.com/perforce/${P4_VERSION}/bin.${P4_ARCH}/p4" \
        -o /usr/local/bin/p4 && break || ([ $attempt -eq 3 ] && exit 1); \
      sleep 2; \
    done; \
    chmod +x /usr/local/bin/p4; \
    p4 -V >/dev/null

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY extract_org_raw_data.py hg_metrics.py p4_metrics.py vcs_extra.py commit_dedup.py code_metrics.py extract_ui.py for_check.py highlight.py pr_signals.py count_merged_prs.py github_app_auth.py clone_all_repos.py LH2-DataLabs.svg ./
COPY tokens.example .

# Bake every tree-sitter grammar the profiler uses into the image. The language pack otherwise
# downloads them at run time, and a failed download silently dropped a language from the counts.
RUN python -c "import code_metrics as m, tree_sitter_language_pack as t; \
names = sorted(set(m.EXT_LANG.values())); t.download(names); \
[t.get_parser(n) for n in names]; print('preloaded', len(names), 'grammars')"

RUN mkdir -p /app/outputs/raw-extracts /data/repos /data/hg-repos

ENV EXTRACT_UI_DOCKER=1 \
    DEFAULT_LOCAL_REPOS_DIR=/data/repos \
    DEFAULT_HG_REPOS_DIR=/data/hg-repos \
    DEFAULT_TOKENS_FILE=/app/secrets/tokens \
    HOST_OUTPUT_HINT=./outputs/raw-extracts \
    PYTHONUNBUFFERED=1 \
    GIT_CONFIG_COUNT=1 \
    GIT_CONFIG_KEY_0=safe.directory \
    GIT_CONFIG_VALUE_0=*

EXPOSE 8766

CMD ["python", "extract_org_raw_data.py", "--ui", "--ui-host", "0.0.0.0", "--ui-port", "8766"]