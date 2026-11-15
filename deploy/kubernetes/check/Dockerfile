# The image of the connection check (connection-check.yaml): Cairnmind from this checkout, with the
# dependency versions and hashes of uv.lock, and connection-check.sh. For the check only; not published.
# scripts/kubernetes_check.py prepares its build folder:
#
#   uv build --wheel -o CONTEXT
#   uv export --locked --no-dev --no-emit-project --format requirements-txt -o CONTEXT/requirements.txt
#   cp deploy/kubernetes/check/connection-check.sh CONTEXT/
#   docker build -t cairnmind-check:local -f deploy/kubernetes/check/Dockerfile CONTEXT

FROM python:3.12-slim
COPY requirements.txt connection-check.sh /tmp/cairnmind/
COPY *.whl /tmp/cairnmind/
RUN set -eu; \
    pip install --no-cache-dir --disable-pip-version-check --require-hashes -r /tmp/cairnmind/requirements.txt; \
    pip install --no-cache-dir --disable-pip-version-check --no-deps /tmp/cairnmind/*.whl; \
    install -m 0755 /tmp/cairnmind/connection-check.sh /usr/local/bin/cairnmind-connection-check; \
    rm -rf /tmp/cairnmind
ENV PYTHON=python3 PYTHONDONTWRITEBYTECODE=1
