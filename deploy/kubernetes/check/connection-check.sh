#!/bin/sh
# The connection check of docs/DEPLOYMENT.md ("Kubernetes"): from a pod that runs Cairnmind, create the
# database and its dedicated user once (signing in as root for that step alone), then connect as that
# user over the server's TLS and check what `cairnmind doctor` sees. Exit status 0 only when every step
# passed. The CI job kubernetes-manifests runs it as the Job in connection-check.yaml.
#
# Environment: the usual Cairnmind settings, all required here:
#   CAIRNMIND_ARCADE_URL            https://arcadedb.cairnmind-db.svc:2490
#   CAIRNMIND_ARCADE_USER           the dedicated user, not root
#   CAIRNMIND_ARCADE_DATABASE       the database name
#   CAIRNMIND_ARCADE_PASSWORD_FILE  the user's password, a mounted Secret
#   CAIRNMIND_ARCADE_CA_FILE        the private authority that signed the server's certificate
# and ARCADEDB_ADMIN_PASSWORD_FILE, the root password (a mounted Secret), used by create-user only.
set -eu

: "${CAIRNMIND_ARCADE_URL:?}" "${CAIRNMIND_ARCADE_USER:?}" "${CAIRNMIND_ARCADE_DATABASE:?}"
: "${CAIRNMIND_ARCADE_PASSWORD_FILE:?}" "${CAIRNMIND_ARCADE_CA_FILE:?}" "${ARCADEDB_ADMIN_PASSWORD_FILE:?}"
python=${PYTHON:-python3}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
data="$work/data"

cairnmind() {
    "$python" -m cairnmind --data-dir "$data" "$@"
}

echo "== create the database and the user $CAIRNMIND_ARCADE_USER, signing in once as root"
cairnmind db create-user --admin-password-file "$ARCADEDB_ADMIN_PASSWORD_FILE" > "$work/user.json"
cat "$work/user.json"

echo "== engine status and doctor as $CAIRNMIND_ARCADE_USER"
cairnmind engine status > "$work/status.json"
cairnmind init > /dev/null
cairnmind doctor > "$work/doctor.json"

"$python" - "$work/status.json" "$work/doctor.json" "$work/user.json" <<'PY'
import json
import os
import sys

status, doctor, user = (json.load(open(path)) for path in sys.argv[1:])
database = doctor["arcadedb"]
print(json.dumps({key: database.get(key) for key in (
    "available", "version", "version_matches", "database", "database_available", "user", "transport",
    "password_source")}, indent=1))
expected = os.environ["CAIRNMIND_ARCADE_USER"]
problems = [text for ok, text in (
    (user.get("signed_in") is True and user.get("user") == expected, "create-user did not sign in as the new user"),
    (status.get("available") and status.get("version_matches"), "engine status does not see the pinned version"),
    (database.get("available") and database.get("version_matches"), "doctor does not see the server"),
    (database.get("database_available"), "doctor cannot read the database"),
    (database.get("user") == expected, "doctor does not use the dedicated user"),
    (database.get("password_source") == "file", "the password does not come from the mounted file"),
    (str(database.get("transport", "")).startswith("https"), "the connection is not over TLS"),
) if not ok]
for text in problems:
    print("FAILED:", text, file=sys.stderr)
sys.exit(1 if problems else 0)
PY
echo "== connection check passed"
