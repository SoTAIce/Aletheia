# Aletheia code-execution sandbox (docs/sandbox_execution_design.md, decision D-014).
# Build: docker build -t aletheia-sandbox:py313 deploy/sandbox
# Runtime limits (network, memory, user, ...) are set by app/tools/sandbox/executor.py,
# not here; this image only provides Python, pytest and an unprivileged user.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install pytest==8.3.4 \
    && useradd --uid 1000 --create-home --shell /usr/sbin/nologin sandbox \
    && mkdir /work && chown sandbox:sandbox /work

USER 1000:1000
WORKDIR /work
CMD ["python", "--version"]
