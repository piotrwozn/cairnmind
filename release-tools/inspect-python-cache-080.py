"""Read-only integrity check of controlled runtime sources and bytecode caches."""
import hashlib
import importlib.util
import json
import marshal
from pathlib import Path
import resource
import struct
import sys
import sysconfig
import tarfile
import types

resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
base = Path.home() / ".cache/devbrain-runtime-080"
runtime = base / "python313/runtime/python"
venv = base / "software313-tmux38/venv"
paths = [runtime / "lib/python3.13/re" / name for name in ("_parser.py", "_compiler.py", "__init__.py")]
paths += [venv / "lib/python3.13/site-packages/textual/css/tokenizer.py"]


def code_map(code):
    result = {code.co_qualname: {"bytecode_sha256": hashlib.sha256(code.co_code).hexdigest(),
                               "firstlineno": code.co_firstlineno, "bytes": len(code.co_code)}}
    for value in code.co_consts:
        if isinstance(value, types.CodeType):
            result.update(code_map(value))
    return result


report = {"python": sys.version, "gil_enabled": sys._is_gil_enabled(),
          "py_gil_disabled": sysconfig.get_config_var("Py_GIL_DISABLED"), "modules": []}
with tarfile.open(base / "python313/python313.tar.gz") as archive:
    for source in paths:
        payload = source.read_bytes()
        cached = Path(importlib.util.cache_from_source(str(source)))
        result = {"source": str(source), "source_sha256": hashlib.sha256(payload).hexdigest(),
                  "cache": str(cached), "cache_exists": cached.exists()}
        if source.is_relative_to(runtime):
            expected = archive.extractfile("python/" + str(source.relative_to(runtime))).read()
            result["source_matches_verified_archive"] = payload == expected
        compiled = code_map(compile(payload, str(source), "exec", optimize=0))
        if cached.exists():
            data = cached.read_bytes()
            result.update(cache_sha256=hashlib.sha256(data).hexdigest(),
                          magic_matches=data[:4] == importlib.util.MAGIC_NUMBER,
                          flags=int.from_bytes(data[4:8], "little"))
            if result["flags"] == 0:
                mtime, size = struct.unpack("<II", data[8:16])
                result["timestamp_header_matches"] = (mtime == int(source.stat().st_mtime) & 0xffffffff
                                                        and size == len(payload))
            on_disk = code_map(marshal.loads(data[16:]))
            result["all_code_objects_match_source_compile"] = on_disk == compiled
        if source.name == "_parser.py":
            result["tokenizer_match"] = compiled["Tokenizer.match"]
        report["modules"].append(result)
output = base / "evidence/python313-cache-integrity.json"
output.write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report))
