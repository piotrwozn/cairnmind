#!/bin/sh
# Start ArcadeDB the way `cairnmind engine start` does (engine.start_engine): the same JVM options
# and server settings, with the paths of the image and an HTTP listener for the container network.
# tests/test_deployment.py keeps the two in step. See docs/DEPLOYMENT.md.
#
# Environment (all optional):
#   ARCADEDB_ROOT_PASSWORD_FILE  file holding the root password  (/run/secrets/arcadedb_root_password)
#   ARCADEDB_DATA                databases, configuration, backups and logs  (/var/lib/arcadedb)
#   ARCADEDB_HTTP_HOST           address the HTTP API listens on inside the container  (0.0.0.0)
#   ARCADEDB_HTTP_PORT           its port  (2480)
#   ARCADEDB_JAVA_OPTS           extra JVM options, after the defaults so they win, e.g. -Xmx4g
set -eu

home=${ARCADEDB_HOME:-/opt/arcadedb}
data=${ARCADEDB_DATA:-/var/lib/arcadedb}
password_file=${ARCADEDB_ROOT_PASSWORD_FILE:-/run/secrets/arcadedb_root_password}
host=${ARCADEDB_HTTP_HOST:-0.0.0.0}
port=${ARCADEDB_HTTP_PORT:-2480}

fail() {
    echo "arcadedb: $*" >&2
    exit 1
}

# No password is ever built into the image: without one ArcadeDB would ask for it on a terminal
# the container does not have.
[ -r "$password_file" ] || fail "no root password: mount a file holding it at $password_file" \
    "(or name it in ARCADEDB_ROOT_PASSWORD_FILE); see docs/DEPLOYMENT.md"
password=$(cat "$password_file")  # without its trailing newline
[ "${#password}" -ge 8 ] || fail "the root password in $password_file is shorter than 8 characters"

if [ "$(id -u)" = 0 ]; then
    # Compose mounts a secret with the owner and mode the file has on the host, so it is read here,
    # as root; the server itself runs as the arcadedb user.
    mkdir -p "$data"
    [ "$(stat -c %u "$data")" = "$(id -u arcadedb)" ] || chown arcadedb:arcadedb "$data"
    handoff=$(mktemp /tmp/arcadedb-root-password.XXXXXX)
    printf '%s' "$password" > "$handoff"
    chown arcadedb:arcadedb "$handoff"
    export ARCADEDB_ROOT_PASSWORD_FILE="$handoff" ARCADEDB_PASSWORD_HANDOFF="$handoff"
    exec setpriv --reuid=arcadedb --regid=arcadedb --init-groups --inh-caps=-all "$0" "$@"
fi

umask 077
for name in config databases backups log; do
    mkdir -p "$data/$name"
done
# The release's default configuration, copied once; later changes in $data/config are kept.
for source in "$home"/config/*; do
    if [ -f "$source" ] && [ ! -e "$data/config/${source##*/}" ]; then
        cp "$source" "$data/config/"
    fi
done

# ArcadeDB reads the password once, when it creates the root user on the first start; later starts
# keep the user it stored in $data/config. The copy holds the password alone, with no newline.
secret=$(mktemp "${TMPDIR:-/tmp}/arcadedb-root-password.XXXXXX")
printf '%s' "$password" > "$secret"
if [ -n "${ARCADEDB_PASSWORD_HANDOFF:-}" ]; then
    rm -f "$ARCADEDB_PASSWORD_HANDOFF"
fi
unset password ARCADEDB_PASSWORD_HANDOFF

cd "$data"
# ARCADEDB_JAVA_OPTS is split into words on purpose.
# shellcheck disable=SC2086
exec java -Xms64m -Xmx1024m --add-modules=jdk.incubator.vector \
    --enable-native-access=ALL-UNNAMED -Djava.awt.headless=true -Dfile.encoding=UTF8 \
    -Darcadedb.profile=low-ram ${ARCADEDB_JAVA_OPTS:-} \
    -Darcadedb.server.rootPath="$data" \
    -Darcadedb.server.configDirectory="$data/config" \
    -Darcadedb.server.databaseDirectory="$data/databases" \
    -Darcadedb.server.backupDirectory="$data/backups" \
    -Darcadedb.server.logsDirectory="$data/log" \
    -Darcadedb.server.rootPasswordPath="$secret" \
    -Darcadedb.server.httpIncomingHost="$host" \
    -Darcadedb.server.httpIncomingPort="$port" \
    -Djava.util.logging.config.file="$data/config/arcadedb-log.properties" \
    -cp "$home/lib/*" com.arcadedb.server.ArcadeDBServer "$@"
