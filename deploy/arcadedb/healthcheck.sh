#!/bin/bash
# Exit 0 when ArcadeDB answers GET /api/v1/ready with a 2xx status. The image has no curl or wget;
# bash's /dev/tcp is enough for one request on the container's own loopback.
set -eu
port=${ARCADEDB_HTTP_PORT:-2480}
exec 3<>"/dev/tcp/127.0.0.1/$port"
printf 'GET /api/v1/ready HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n' >&3
IFS= read -r status <&3
case "$status" in
    "HTTP/1."[01]" 2"[0-9][0-9]*) exit 0 ;;
    *) echo "ArcadeDB is not ready: ${status:-no answer}" >&2; exit 1 ;;
esac
