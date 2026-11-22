#!/bin/sh
# The connection check of docs/DEPLOYMENT.md ("Kubernetes"): from a pod that runs Cairnmind, create the
# database and its dedicated user once (signing in as root for that step alone), then connect as that
# user over the server's TLS and check what `cairnmind doctor` sees. Exit status 0 only when every step
# passed. The CI job kubernetes-manifests runs it as the Job in connection-check.yaml.
#
# The installation is connect-only (#68): it never installs or starts an engine, here or anywhere.
#
# Environment: the usual Cairnmind settings, all required here:
#   CAIRNMIND_ARCADE_CONNECT_URL    https://arcadedb.cairnmind-db.svc:2490
#   CAIRNMIND_ARCADE_USER           the dedicated user, not root
#   CAIRNMIND_ARCADE_DATABASE       the database name
#   CAIRNMIND_ARCADE_PASSWORD_FILE  the user's password, a mounted Secret
#   CAIRNMIND_ARCADE_CA_FILE        the private authority that signed the server's certificate
# and ARCADEDB_ADMIN_PASSWORD_FILE, the root password (a mounted Secret), used by create-user only.
set -eu

: "${CAIRNMIND_ARCADE_CONNECT_URL:?}" "${CAIRNMIND_ARCADE_USER:?}" "${CAIRNMIND_ARCADE_DATABASE:?}"
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
cairnmind doctor > "$work/doctor.json" || [ $? -eq 1 ]  # 1: something required is missing; the checks below judge the report (#313)

echo "== index a small project from inside the cluster, and read it back"
mkdir "$work/project"
printf 'def answer():\n    return 42\n' > "$work/project/answer.py"
cairnmind project add in-cluster "$work/project" > "$work/project.json"

echo "== engine commands say that nothing is managed here"
if cairnmind engine start > "$work/start.txt" 2>&1; then
    echo "FAILED: engine start did not refuse" >&2
    exit 1
fi
grep -q "only connects to ArcadeDB" "$work/start.txt" || { cat "$work/start.txt" >&2; exit 1; }
[ ! -e "$data/arcade" ] && [ ! -e "$data/runtime" ] || { echo "FAILED: engine files in $data" >&2; exit 1; }

"$python" - "$work/status.json" "$work/doctor.json" "$work/user.json" "$work/project.json" <<'PY'
import json
import os
import sys

status, doctor, user, project = (json.load(open(path)) for path in sys.argv[1:])
database = doctor["arcadedb"]
print(json.dumps({key: database.get(key) for key in (
    "mode", "host", "available", "version", "version_matches", "database", "database_available", "user",
    "transport", "password_source")}, indent=1))
expected = os.environ["CAIRNMIND_ARCADE_USER"]
problems = [text for ok, text in (
    (user.get("signed_in") is True and user.get("user") == expected, "create-user did not sign in as the new user"),
    (status.get("available") and status.get("version_matches"), "engine status does not see the pinned version"),
    (database.get("available") and database.get("version_matches"), "doctor does not see the server"),
    (database.get("database_available"), "doctor cannot read the database"),
    (database.get("user") == expected, "doctor does not use the dedicated user"),
    (database.get("password_source") == "file", "the password does not come from the mounted file"),
    (str(database.get("transport", "")).startswith("https"), "the connection is not over TLS"),
    (status.get("mode") == "connect" and database.get("mode") == "connect", "the installation is not connect-only"),
    ("java" not in status and doctor.get("java", {}).get("needed") is False, "Java was looked for"),
    (project.get("indexed", {}).get("changed") == 1, "the project was not indexed"),
) if not ok]
for text in problems:
    print("FAILED:", text, file=sys.stderr)
sys.exit(1 if problems else 0)
PY
echo "== connection check passed"
