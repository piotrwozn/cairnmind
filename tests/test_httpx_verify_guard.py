"""Every httpx client under src/cairnmind states its `verify=`, so a plain-http client builds no certificate store (#415)."""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "cairnmind"
CALLS = {"Client", "AsyncClient", "get", "post", "request", "stream", "put", "delete", "patch", "head"}
# (module, function) -> reason a call without `verify=` is right. Only calls that must always verify belong here.
ALLOWED: dict[tuple[str, str], str] = {}


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
    missing, literal = [], []
    for path in sorted(SRC.glob("*.py")):
        for function, call in _calls(path):
            where = f"{path.name}:{call.lineno} {function}"
            keywords = {k.arg: k.value for k in call.keywords}
            if "verify" not in keywords:
                if (path.name, function) not in ALLOWED:
                    missing.append(where)
            elif isinstance(keywords["verify"], ast.Constant) and keywords["verify"].value is False:
                literal.append(where)
    assert not literal, "verify=False written out; use config.tls_verify(url):\n" + "\n".join(literal)
    assert not missing, "httpx call without verify= (use verify=tls_verify(url)):\n" + "\n".join(missing)
