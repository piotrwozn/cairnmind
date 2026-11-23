"""Every httpx client under src/cairnmind states its `verify=`, so a plain-http client builds no certificate store (#415)."""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "cairnmind"
CALLS = {"Client", "AsyncClient", "get", "post", "request", "stream", "put", "delete", "patch", "head"}
# (module, function) -> reason a call without `verify=` is right. Only calls that must always verify belong here.
ALLOWED: dict[tuple[str, str], str] = {}
# module -> the `verify=` expressions its clients may state, each naming the address that client talks to. A client whose
# address can be https must verify; tls_verify(url) says so only for the url it is given, so the url is the very one
# the client asks. A new site, or a different url at an old one, fails here until it is read and added.
VERIFY: dict[str, set[str]] = {
    "agent_memory.py": {"tls_verify(self.settings.url)"},
    "api_providers.py": {"tls_verify(self.profile['base_url'])"},  # a profile is fixed when the Provider is made
    "api_runtime.py": {"tls_verify(self.settings.url)", "tls_verify(url)"},  # url: the broker, http on 127.0.0.1 only
    "arcade.py": {"self._verify"},  # an ssl context for https (tls_context), False only for the loopback http it is given
    "cli.py": {"tls_verify(settings.url)", "tls_verify(settings.ollama_url)"},
    "client_runtime.py": {"tls_verify(self.settings.url)"},
    "cockpit.py": {"tls_verify(settings.url)", "tls_verify(info['url'])", "tls_verify(status['url'])"},
    "integrations.py": {"doctor_verify(settings)", "tls_verify(settings.url)"},
    "knowledge_mode.py": {"doctor_verify(settings)"},
    "mcp_server.py": {"tls_verify(self.settings.url)"},
    "ollama.py": {"tls_verify(self.base_url)"},
    "service_version.py": {"tls_verify(settings.url)"},
    "setup_change.py": {"tls_verify(settings.url)"},
    "terminal_client.py": {"tls_verify(url)"},  # BrokerClient._validated: http on 127.0.0.1 only
    "topology.py": {"doctor_verify(settings)"},
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
            elif ast.unparse(keywords["verify"]) not in VERIFY.get(path.name, ()):
                unlisted.append(f"{where}: verify={ast.unparse(keywords['verify'])}")
    assert not literal, "verify=False written out; use config.tls_verify(url):\n" + "\n".join(literal)
    assert not missing, "httpx call without verify= (use verify=tls_verify(url)):\n" + "\n".join(missing)
    assert not unlisted, "verify= is not tls_verify(<its own address>), doctor_verify(settings) or listed:\n" + "\n".join(unlisted)
    assert not redirected, "an httpx client follows redirects:\n" + "\n".join(redirected)


def test_every_listed_verify_expression_is_still_used():
    used = {(path.name, ast.unparse({k.arg: k.value for k in call.keywords}["verify"]))
            for path in SRC.glob("*.py") for _, call in _calls(path) if any(k.arg == "verify" for k in call.keywords)}
    stale = {(name, text) for name, texts in VERIFY.items() for text in texts} - used
    assert not stale, f"listed but not used, remove: {sorted(stale)}"


def test_no_httpx_client_is_made_through_a_name_imported_from_httpx():
    # `from httpx import Client` would make a client the checks above cannot see.
    for path in SRC.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module == "httpx":
                assert not {a.name for a in node.names} & (CALLS | {"Client", "AsyncClient"}), path.name
