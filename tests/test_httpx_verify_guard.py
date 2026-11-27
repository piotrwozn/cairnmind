"""Every httpx client under src/cairnmind states its `verify=`, so a plain-http client builds no certificate store (#415)."""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "cairnmind"
CALLS = {"Client", "AsyncClient", "HTTPTransport", "AsyncHTTPTransport", "get", "post", "request", "stream", "put", "delete", "patch", "head"}
# (module, function) -> reason a call without `verify=` is right. Only calls that must always verify belong here.
ALLOWED: dict[tuple[str, str], str] = {}
# (module, function) -> the one `verify=` expression its client may state. This pins the FORM of verify= (a literal False
# or a bare variable fails); it does not compare the expression with the address the client asks. That pairing is read
# per site and pinned by behaviour in test_tls_verify_sites.py, so a site whose address can vary must be added to its SITES.
VERIFY: dict[tuple[str, str], str] = {
    ("agent_memory.py", "_request"): "tls_verify(self.settings.url)",
    ("api_providers.py", "_http"): "tls_verify(self.profile['base_url'])",  # a profile is fixed when the Provider is made
    ("api_runtime.py", "_prepare_session"): "tls_verify(self.settings.url)",
    ("api_runtime.py", "api_worker_config"): "tls_verify(url)",  # the broker: http on 127.0.0.1 only
    ("api_runtime.py", "report_mcp_failure"): "tls_verify(url)",
    ("arcade.py", "client"): "self._verify",  # an ssl context for https (tls_context), False only for the loopback http it is given
    ("cli.py", "service_alive"): "tls_verify(settings.url)",
    ("cli.py", "_stop_started"): "tls_verify(settings.url)",
    ("cli.py", "request"): "tls_verify(settings.url)",
    ("cli.py", "model_pull"): "tls_verify(settings.ollama_url)",
    ("client_runtime.py", "_memory_identity"): "tls_verify(self.settings.url)",
    ("cockpit.py", "upstream"): "tls_verify(settings.url)",
    ("cockpit.py", "stream"): "tls_verify(settings.url)",
    ("cockpit.py", "cockpit_status"): "tls_verify(info['url'])",
    ("cockpit.py", "stop_cockpit"): "tls_verify(status['url'])",
    ("integrations.py", "doctor"): "doctor_verify(settings)",
    ("integrations.py", "_hook_startup_context"): "tls_verify(settings.url)",
    ("knowledge_mode.py", "missing_models"): "doctor_verify(settings)",
    ("mcp_server.py", "_client"): "tls_verify(self.settings.url)",
    ("migration.py", "_notify_service_reload"): "tls_verify(settings.url)",
    ("ollama.py", "_new_client"): "tls_verify(self.base_url)",
    ("service_version.py", "refusal"): "tls_verify(settings.url)",
    ("setup_change.py", "_stop"): "tls_verify(settings.url)",
    ("terminal_client.py", "_connect"): "tls_verify(url)",  # BrokerClient._validated: http on 127.0.0.1 only
    ("terminal_client.py", "events"): "tls_verify(url)",
    ("topology.py", "probe"): "doctor_verify(settings)",
}

def _calls(path: Path):
    """Yield (function name, call) for each httpx client or direct request call in one module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    stack: list[str] = []

    def walk(node):
        named = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        if named:
            stack.append(node.name)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in CALLS
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "httpx"):
            yield (stack[-1] if stack else "<module>"), node
        for child in ast.iter_child_nodes(node):
            yield from walk(child)
        if named:
            stack.pop()

    yield from walk(tree)


def test_every_httpx_client_states_verify_and_none_turns_it_off_by_a_literal():
    missing, literal, unlisted, redirected = [], [], [], []
    for path in sorted(SRC.glob("*.py")):
        for function, call in _calls(path):
            where = f"{path.name}:{call.lineno} {function}"
            keywords = {k.arg: k.value for k in call.keywords}
            if isinstance(keywords.get("follow_redirects"), ast.Constant) and keywords["follow_redirects"].value:
                redirected.append(where)  # a plain-http client that followed one could reach an https host unverified
            if "verify" not in keywords:
                if (path.name, function) not in ALLOWED:
                    missing.append(where)
            elif isinstance(keywords["verify"], ast.Constant) and keywords["verify"].value is False:
                literal.append(where)
            elif ast.unparse(keywords["verify"]) != VERIFY.get((path.name, function)):
                unlisted.append(f"{where}: verify={ast.unparse(keywords['verify'])}")
    assert not literal, "verify=False written out; use config.tls_verify(url):\n" + "\n".join(literal)
    assert not missing, "httpx call without verify= (use verify=tls_verify(url)):\n" + "\n".join(missing)
    assert not unlisted, "verify= is not tls_verify(<its own address>), doctor_verify(settings) or listed:\n" + "\n".join(unlisted)
    assert not redirected, "an httpx client follows redirects:\n" + "\n".join(redirected)


def test_every_listed_verify_expression_is_still_used():
    used = {((path.name, function), ast.unparse({k.arg: k.value for k in call.keywords}["verify"]))
            for path in SRC.glob("*.py") for function, call in _calls(path) if any(k.arg == "verify" for k in call.keywords)}
    stale = set(VERIFY.items()) - used
    assert not stale, f"listed but not used, remove: {sorted(stale)}"


def test_no_httpx_client_is_made_through_a_name_imported_from_httpx():
    # `from httpx import Client` or `import httpx as hx` would make a client the checks above cannot see.
    for path in SRC.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module == "httpx":
                assert not {a.name for a in node.names} & CALLS, path.name
            if isinstance(node, ast.Import):
                assert all(a.name != "httpx" or a.asname in (None, "httpx") for a in node.names), path.name
