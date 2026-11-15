# ArcadeDB for Cairnmind: the release Cairnmind pins (Settings.arcade_version in
# src/cairnmind/config.py; tests/test_deployment.py keeps the two equal), on Java 21, started with the
# options of `cairnmind engine start`. No password is built in: the server reads it from a file
# mounted at run time. See docs/DEPLOYMENT.md.
#
#   docker build -t cairnmind-arcadedb:local deploy/arcadedb

ARG ARCADEDB_VERSION=26.11.1

FROM eclipse-temurin:21-jre-noble AS download
ARG ARCADEDB_VERSION
WORKDIR /download
# The same release archive and published SHA-256 that engine.install_engine downloads and checks.
RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates; \
    name="arcadedb-${ARCADEDB_VERSION}-base.tar.gz"; \
    base="https://github.com/ArcadeData/arcadedb/releases/download/${ARCADEDB_VERSION}"; \
    curl -fsSL --retry 3 -o "$name" "$base/$name"; \
    curl -fsSL --retry 3 -o "$name.sha256" "$base/$name.sha256"; \
    expected="$(awk '{print $1; exit}' "$name.sha256")"; \
    test "${#expected}" -eq 64; \
    echo "$expected  $name" | sha256sum -c -; \
    tar -xzf "$name"; \
    test -d "arcadedb-${ARCADEDB_VERSION}/lib"; \
    mv "arcadedb-${ARCADEDB_VERSION}" /opt/arcadedb

FROM eclipse-temurin:21-jre-noble
ARG ARCADEDB_VERSION
LABEL org.opencontainers.image.title="ArcadeDB for Cairnmind" \
      org.opencontainers.image.version="${ARCADEDB_VERSION}" \
      org.opencontainers.image.source="https://github.com/piotrwozn/cairnmind" \
      org.opencontainers.image.licenses="Apache-2.0"
RUN set -eu; \
    groupadd --system --gid 10001 arcadedb; \
    useradd --system --uid 10001 --gid arcadedb --home-dir /var/lib/arcadedb --no-create-home \
        --shell /usr/sbin/nologin arcadedb; \
    mkdir -p /var/lib/arcadedb; \
    chown arcadedb:arcadedb /var/lib/arcadedb; \
    chmod 0700 /var/lib/arcadedb
COPY --from=download /opt/arcadedb /opt/arcadedb
COPY --chmod=0755 entrypoint.sh healthcheck.sh /opt/arcadedb/
ENV ARCADEDB_HOME=/opt/arcadedb \
    ARCADEDB_DATA=/var/lib/arcadedb \
    ARCADEDB_ROOT_PASSWORD_FILE=/run/secrets/arcadedb_root_password \
    ARCADEDB_HTTP_PORT=2480
VOLUME ["/var/lib/arcadedb"]
EXPOSE 2480
# The entrypoint starts as root only to read a secret mounted with the host's owner and mode, then
# runs the server as arcadedb (uid 10001). With `--user 10001` it starts as arcadedb directly.
ENTRYPOINT ["/opt/arcadedb/entrypoint.sh"]
HEALTHCHECK --interval=5s --timeout=3s --start-period=60s --retries=24 CMD ["/opt/arcadedb/healthcheck.sh"]
