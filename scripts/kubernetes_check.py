"""Check the Kubernetes manifests of docs/DEPLOYMENT.md on a cluster, as the `kubernetes-manifests` CI job does.

    kind create cluster --name cairnmind-check --wait 180s
    uv run python scripts/kubernetes_check.py --kind-cluster cairnmind-check

Uses the current kubectl context, which must be a disposable cluster: the namespaces cairnmind-db,
cairnmind-check and cairnmind-app are created and deleted at the end (keep them with --keep). Needs docker, kubectl,
openssl and uv; with --kind-cluster, also kind, to load the two images into that cluster.

  1. builds deploy/arcadedb and the check image of deploy/kubernetes/check;
  2. creates the namespaces, then checks every manifest under deploy/kubernetes against the cluster's
     schemas (kubectl apply --dry-run=server --validate=strict), the Secret template included;
  3. makes a private certificate authority, the server's TLS store and the passwords, and creates the
     Secrets from files, as docs/DEPLOYMENT.md does;
  4. applies deploy/kubernetes and waits until the StatefulSet is ready (its probes are the image's
     health check);
  5. runs the connection check as the Job of deploy/kubernetes/check, a connect-only installation
     inside the cluster: create-user as root once, then engine status, doctor and indexing a small project
     as the dedicated user over TLS, both passwords read from mounted Secrets;
  6. runs `cairnmind engine status` and `doctor` from this machine through kubectl port-forward, as a
     second connect-only installation: it reads only, takes over writing as a person would, then writes a
     project and a record, takes a logical backup and restores it, as docs/DEPLOYMENT.md does;
  7. scales the server to 0, archives its volume with tar from a pod that holds it, unpacks the archive
     into a new, empty volume and reads the same records from the server started on it;
  8. reports whether the network plugin enforces the NetworkPolicy: a pod without the client label is
     kept off 2490, a labelled one reaches 2490 but not the plain HTTP port 2480 (informational).
No model, Ollama or coding client is used. Exit status 0 only when steps 1 to 7 passed.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = ROOT / "deploy" / "kubernetes"
CHECK = MANIFESTS / "check"
SERVER_IMAGE, CHECK_IMAGE = "cairnmind-arcadedb:local", "cairnmind-check:local"
DATABASE, CLIENTS = "cairnmind-db", "cairnmind-check"
# The placeholder client namespace of templates/secrets.yaml: a server-side dry run needs it to exist.
TEMPLATE_CLIENTS = "cairnmind-app"
JOB = "cairnmind-connection-check"


def step(message: str) -> None:
    print(f"== {message}", flush=True)


def run(*command: str, check: bool = True, capture: bool = False, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=check, text=True, capture_output=capture, **kwargs)


def kubectl(*arguments: str, **kwargs) -> subprocess.CompletedProcess:
    return run("kubectl", *arguments, **kwargs)


def build_images(work: Path, kind_cluster: str | None) -> None:
    step(f"build {SERVER_IMAGE} and {CHECK_IMAGE}")
    run("docker", "build", "-t", SERVER_IMAGE, str(ROOT / "deploy" / "arcadedb"))
    context = work / "check-image"
    run("uv", "build", "--wheel", "-o", str(context), cwd=ROOT)
    run("uv", "export", "--locked", "--no-dev", "--no-emit-project", "--format", "requirements-txt",
        "-o", str(context / "requirements.txt"), cwd=ROOT)
    shutil.copy(CHECK / "connection-check.sh", context)
    run("docker", "build", "-t", CHECK_IMAGE, "-f", str(CHECK / "Dockerfile"), str(context))
    if kind_cluster:
        for image in (SERVER_IMAGE, CHECK_IMAGE):
            run("kind", "load", "docker-image", image, "--name", kind_cluster)


def schema_check() -> None:
    step("namespaces, then every manifest against the cluster's schemas")
    kubectl("apply", "-f", str(MANIFESTS / "00-namespace.yaml"))
    kubectl("create", "namespace", CLIENTS)
    kubectl("create", "namespace", TEMPLATE_CLIENTS)
    manifests = sorted(MANIFESTS.rglob("*.yaml"))
    for path in manifests:
        kubectl("apply", "--dry-run=server", "--validate=strict", "-f", str(path))
    print(f"{len(manifests)} manifest files are valid")


def openssl(*arguments: str, cwd: Path) -> None:
    run("openssl", *arguments, cwd=cwd, capture=True)


def make_secrets(work: Path) -> dict[str, Path]:
    """The files docs/DEPLOYMENT.md has the operator make, and the Secrets made from them."""
    step("a private authority, the server's TLS store, the passwords and the Secrets")
    folder = work / "secrets"
    folder.mkdir(mode=0o700)
    openssl("req", "-x509", "-newkey", "rsa:3072", "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "2",
            "-subj", "/CN=Cairnmind check authority", "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign", cwd=folder)
    openssl("req", "-newkey", "rsa:3072", "-nodes", "-keyout", "tls.key", "-out", "tls.csr", "-subj", "/CN=arcadedb",
            cwd=folder)
    names = ["arcadedb", f"arcadedb.{DATABASE}", f"arcadedb.{DATABASE}.svc", f"arcadedb.{DATABASE}.svc.cluster.local",
             "localhost"]
    (folder / "tls.ext").write_text("subjectAltName=" + ",".join([f"DNS:{name}" for name in names] + ["IP:127.0.0.1"])
                                    + "\nbasicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n")
    openssl("x509", "-req", "-in", "tls.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-days", "2",
            "-out", "tls.crt", "-extfile", "tls.ext", cwd=folder)
    files = {}
    for name in ("store-password", "root-password", "user-password"):
        files[name] = folder / name
        files[name].write_text(secrets.token_hex(24))  # no newline: the entrypoint reads it as it is
        files[name].chmod(0o600)
    openssl("pkcs12", "-export", "-in", "tls.crt", "-inkey", "tls.key", "-certfile", "ca.pem", "-name", "arcadedb",
            "-out", "keystore.p12", "-passout", f"file:{files['store-password']}", cwd=folder)
    files["ca"], files["keystore"] = folder / "ca.pem", folder / "keystore.p12"
    kubectl("-n", DATABASE, "create", "secret", "generic", "arcadedb-root-password",
            f"--from-file=password={files['root-password']}")
    kubectl("-n", DATABASE, "create", "secret", "generic", "arcadedb-tls",
            f"--from-file=keystore.p12={files['keystore']}", f"--from-file=store-password={files['store-password']}")
    kubectl("-n", CLIENTS, "create", "secret", "generic", "arcadedb-root-password",
            f"--from-file=password={files['root-password']}")
    kubectl("-n", CLIENTS, "create", "secret", "generic", "cairnmind-db-user",
            f"--from-file=password={files['user-password']}")
    kubectl("-n", CLIENTS, "create", "configmap", "arcadedb-ca", f"--from-file=ca.pem={files['ca']}")
    return files


def start_server() -> None:
    step("apply deploy/kubernetes and wait for the StatefulSet")
    kubectl("apply", "-f", str(MANIFESTS))
    kubectl("-n", DATABASE, "rollout", "status", "statefulset/arcadedb", "--timeout=300s")
    mounted = kubectl("-n", DATABASE, "exec", "arcadedb-0", "--", "stat", "-L", "-c", "%a %u:%g",
                      "/run/secrets/arcadedb-root/password", capture=True, check=False)
    print(f"the server's mounted root password file: mode, owner:group = {mounted.stdout.strip() or mounted.stderr}")


def connection_job() -> None:
    step("the connection check as a Job, with the passwords mounted from Secrets")
    kubectl("apply", "-f", str(CHECK / "connection-check.yaml"))
    deadline = time.monotonic() + 360
    state = ""
    while time.monotonic() < deadline:
        state = kubectl("-n", CLIENTS, "get", "job", JOB, "-o",
                        "jsonpath={.status.succeeded}/{.status.failed}", capture=True).stdout
        if state.startswith("1/") or state.endswith("/1"):
            break
        time.sleep(3)
    kubectl("-n", CLIENTS, "logs", f"job/{JOB}", check=False)
    if not state.startswith("1/"):
        kubectl("-n", CLIENTS, "describe", "job", JOB, check=False)
        raise SystemExit(f"the connection check did not pass (succeeded/failed: {state or 'none'})")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def forwarded(files: dict[str, Path], data: Path):
    """`cairnmind` with a data directory of its own, reaching the server through kubectl port-forward as
    the dedicated user, as docs/DEPLOYMENT.md does from a workstation."""
    port = free_port()
    forward = subprocess.Popen(["kubectl", "-n", DATABASE, "port-forward", "svc/arcadedb", f"{port}:2490"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                if time.monotonic() > deadline or forward.poll() is not None:
                    raise SystemExit("kubectl port-forward did not open its port") from None
                time.sleep(0.5)
        env = {key: value for key, value in os.environ.items() if not key.startswith(("CAIRNMIND_", "DEVBRAIN_"))}
        env.update(CAIRNMIND_ARCADE_CONNECT_URL=f"https://localhost:{port}", CAIRNMIND_ARCADE_USER="cairnmind",
                   CAIRNMIND_ARCADE_DATABASE="cairnmind", CAIRNMIND_ARCADE_PASSWORD_FILE=str(files["user-password"]),
                   CAIRNMIND_ARCADE_CA_FILE=str(files["ca"]))

        def cairnmind(*arguments: str, codes: tuple[int, ...] = (0,)) -> dict:
            result = run(sys.executable, "-m", "cairnmind", "--data-dir", str(data), *arguments, env=env,
                         capture=True, check=False, timeout=300)
            if result.returncode not in codes:
                raise SystemExit(f"cairnmind {' '.join(arguments)} failed:\n{result.stdout}{result.stderr}")
            return json.loads(result.stdout)

        cairnmind.env = env
        yield cairnmind
    finally:
        forward.terminate()
        forward.wait(10)


def port_forward_check(files: dict[str, Path], work: Path) -> list:
    step("engine status and doctor from this machine through kubectl port-forward")
    with forwarded(files, work / "port-forward-data") as cairnmind:
        status = cairnmind("engine", "status")
        # A reader of the Job's store before it takes over: doctor exits 1 when something required is missing (#313).
        database = cairnmind("doctor", codes=(0, 1))["arcadedb"]
        if not (status.get("available") and status.get("version_matches") and database.get("database_available")
                and database.get("transport", "").startswith("https")):
            raise SystemExit(f"through port-forward: engine status {status}, doctor {database}")
        if status.get("mode") != "connect" or "java" in status:
            raise SystemExit(f"through port-forward: the installation is not connect-only: {status}")
        print(f"through port-forward: {database.get('user')} on {database.get('database')}, {database.get('transport')}, "
              "connect-only")
        step("a second installation reads only, and a person's takeover moves writing to it")
        cairnmind("init")
        # The Job's installation, in the cluster, opened the database first and is its writer (#70).
        role = cairnmind("store", "writer")
        if role.get("role") != "reader":
            raise SystemExit(f"the workstation's installation is not a reader of the Job's store: {role}")
        refused = run(sys.executable, "-m", "cairnmind", "--data-dir", str(work / "port-forward-data"), "remember",
                      "in-cluster", "A reader must not write this.", env=cairnmind.env, capture=True, check=False)
        if refused.returncode == 0 or "only reads this store" not in refused.stdout + refused.stderr:
            raise SystemExit(f"a reader's write was not refused:\n{refused.stdout}{refused.stderr}")
        taken = cairnmind("store", "takeover", "--yes", "--note", "the Kubernetes check moves writing to the workstation")
        if not taken.get("taken_over") or cairnmind("store", "writer").get("role") != "writer":
            raise SystemExit(f"the takeover did not make the workstation the writer: {taken}")
        print(f"the workstation took over from installation {str(taken.get('previous'))[:8]}")
        step("a logical backup and its restore as the dedicated user, through the same port-forward")
        tree = work / "demo"
        tree.mkdir()
        (tree / "README.md").write_text("# Demo\n\nA project for the Kubernetes check.\n")
        cairnmind("project", "add", "demo", str(tree))
        # Not --confirmed: the command line confirms nothing, a person does in the workspace.
        cairnmind("remember", "demo", "The Kubernetes check wrote this record.", "--key", "k8s-check")
        before = cairnmind("records", "demo")
        backup = cairnmind("backup", str(work / "backup.json.gz"))["backup"]
        cairnmind("restore", backup)
        after = cairnmind("records", "demo")
        if after != before or not any(record.get("key") == "k8s-check" for record in after):
            raise SystemExit(f"the restored records differ:\n{json.dumps([before, after], indent=1)}")
        print(f"backed up and restored {len(after)} record(s)")
        return after


def volume_pod(*arguments: str, **kwargs) -> subprocess.CompletedProcess:
    return kubectl("-n", DATABASE, "exec", "-i", "arcadedb-volume", "--", *arguments, **kwargs)


def hold_volume() -> None:
    kubectl("apply", "-f", str(MANIFESTS / "backup" / "volume-pod.yaml"))
    kubectl("-n", DATABASE, "wait", "--for=condition=Ready", "pod/arcadedb-volume", "--timeout=180s")


def release_volume() -> None:
    kubectl("-n", DATABASE, "delete", "pod", "arcadedb-volume", "--wait=true", "--timeout=120s")


def volume_snapshot(files: dict[str, Path], work: Path, expected: list) -> None:
    """The volume snapshot of docs/DEPLOYMENT.md: archive the volume of the stopped server, unpack it into
    a new, empty volume, start the server on it and read the same records."""
    step("volume snapshot of the stopped server, unpacked into a new volume")
    kubectl("-n", DATABASE, "scale", "statefulset/arcadedb", "--replicas=0")
    kubectl("-n", DATABASE, "wait", "--for=delete", "pod/arcadedb-0", "--timeout=180s")
    hold_volume()
    archive = work / "arcadedb-volume.tgz"
    with archive.open("wb") as handle:
        volume_pod("tar", "--numeric-owner", "-C", "/var/lib/arcadedb", "-czf", "-", ".", stdout=handle,
                   stdin=subprocess.DEVNULL)
    release_volume()
    kubectl("-n", DATABASE, "delete", "pvc", "data-arcadedb-0", "--wait=true", "--timeout=120s")
    kubectl("apply", "-f", str(MANIFESTS / "backup" / "data-claim.yaml"))
    hold_volume()
    with archive.open("rb") as handle:
        # The pod runs as 10001 and the new volume's top folder belongs to root: --no-overwrite-dir leaves
        # that folder's mode and time alone instead of failing to set them from the archive's "." entry.
        volume_pod("tar", "--numeric-owner", "--no-overwrite-dir", "-C", "/var/lib/arcadedb", "-xzf", "-",
                   stdin=handle)
    release_volume()
    kubectl("-n", DATABASE, "scale", "statefulset/arcadedb", "--replicas=1")
    kubectl("-n", DATABASE, "rollout", "status", "statefulset/arcadedb", "--timeout=300s")
    with forwarded(files, work / "snapshot-data") as cairnmind:
        cairnmind("init")
        found = cairnmind("records", "demo")
    if found != expected:
        raise SystemExit(f"the server on the unpacked volume has other records:\n{json.dumps(found, indent=1)}")
    print(f"the new volume holds the same {len(found)} record(s); {archive.stat().st_size} bytes archived")


def reach(name: str, port: int, *, client: bool) -> str:
    """'open' or 'blocked ...': a TCP connection from a new pod in the client namespace to the server's
    pod (its own address, so that the port the Service does not offer is tried as well)."""
    probe = (f"import socket\ntry:\n socket.create_connection(('arcadedb-0.arcadedb.{DATABASE}.svc',{port}),timeout=5)"
             ".close()\n print('open')\nexcept OSError as e:\n print('blocked', type(e).__name__)")
    labels = ["--labels=cairnmind.io/database-client=true"] if client else []
    result = kubectl("-n", CLIENTS, "run", name, "--rm", "-i", "--restart=Never", "--quiet", *labels,
                     f"--image={CHECK_IMAGE}", "--image-pull-policy=IfNotPresent", "--command", "--",
                     "python3", "-c", probe, capture=True, check=False, timeout=180)
    return (result.stdout or result.stderr).strip() or "no answer"


def network_policy_report() -> None:
    step("does the network plugin enforce the NetworkPolicy? (informational)")
    answers = {(client, port): reach(f"policy-probe-{'client' if client else 'other'}-{port}", port, client=client)
               for client, port in ((False, 2490), (True, 2490), (True, 2480))}
    for (client, port), answer in answers.items():
        who = "a pod labelled as a client" if client else "a pod without the client label"
        print(f"{who}, port {port}: {answer}", flush=True)
    enforced = (answers[(False, 2490)].startswith("blocked") and answers[(True, 2490)] == "open"
                and answers[(True, 2480)].startswith("blocked"))
    print("policy enforced: only labelled pods, only on 2490" if enforced
          else "policy NOT enforced as written by this cluster's network plugin", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--kind-cluster", help="load the images into this kind cluster")
    parser.add_argument("--keep", action="store_true", help="keep the namespaces at the end")
    args = parser.parse_args()
    context = kubectl("config", "current-context", capture=True).stdout.strip()
    print(f"kubectl context: {context}")
    with tempfile.TemporaryDirectory(prefix="cairnmind-kubernetes-check-") as temporary:
        work = Path(temporary)
        try:
            build_images(work, args.kind_cluster)
            schema_check()
            files = make_secrets(work)
            start_server()
            connection_job()
            records = port_forward_check(files, work)
            volume_snapshot(files, work, records)
            network_policy_report()
        except (subprocess.CalledProcessError, SystemExit):
            kubectl("-n", DATABASE, "get", "all,pvc,events", check=False)
            kubectl("-n", DATABASE, "logs", "statefulset/arcadedb", "--tail=200", check=False)
            raise
        finally:
            if not args.keep:
                kubectl("delete", "namespace", DATABASE, CLIENTS, TEMPLATE_CLIENTS, "--ignore-not-found", "--wait=false",
                        check=False)
    step("passed")


if __name__ == "__main__":
    main()
