# run-agent image for the "Monad of No Return" refactor pipeline.
#
# Deliberately toolchain-free: this container only *applies* the precomputed
# patch (deterministic, trusted) and then asks the isolated Sandbox to build.
# It never compiles untrusted library code itself, so it needs just python3 +
# git -- no GHC/cabal, no credentials, no k8s token.
#
# Build from the REPO ROOT so both the script and the patch set are in context:
#   docker build -f agent/Dockerfile -t ghcr.io/<owner>/refactor-agent:latest .
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY agent/refactor.py /app/refactor.py
COPY auto.diff /app/auto.diff

# Defaults match the Argo run-agent step; override via env/args.
ENV DIFF_FILE=/app/auto.diff \
    WORKDIR=/workspace/repo \
    RESULT_PATH=/workspace/result.json

ENTRYPOINT ["python3", "/app/refactor.py"]
CMD ["run"]
