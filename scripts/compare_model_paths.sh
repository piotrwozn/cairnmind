#!/usr/bin/env bash
# Compare the chat model through the native Ollama path and through the OpenAI-compatible /v1 endpoint (#217).
# Three runs of `eval run` and three of `eval repository` on each path, then one table. Run it yourself, in WSL.
set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: bash scripts/compare_model_paths.sh DATA_DIR REPORT_DIR V1_URL [REPOSITORY]

DATA_DIR    A prepared throwaway data directory (`cairnmind init`) whose config.json sets its own "port" and
            "arcade_url" (not 8765 and not 2480), with its engine already running. Never the installation's.
REPORT_DIR  Where the 12 reports and the table input are written.
V1_URL      Ollama's OpenAI-compatible address, e.g. http://127.0.0.1:11435/v1, of a second `ollama serve` started with
            OLLAMA_CONTEXT_LENGTH=16384: Cairnmind sends num_ctx only on the native path, so that server's own context
            must equal the native one (16384), or the /v1 series measures a smaller context, not the path.
REPOSITORY  The checkout to ask about (default: the current directory).

The native series uses the ollama_url of DATA_DIR. Only the chat role goes through /v1 (--reasoning-effort none,
--context-window 16384); the embedding role stays native. Afterwards the chat role is set back to the local Ollama.
It starts no engine and no Ollama, and calls the models for about an hour. It is not run by the test suite.
USAGE
}

if [ "${1:-}" = "--help" ] || [ $# -lt 3 ]; then usage; exit 2; fi
# No realpath: macOS has none with -m. The paths stay relative to here; the check below resolves DATA_DIR itself.
DATA_DIR=$1; REPORT_DIR=$2; V1_URL=$3; REPOSITORY=${4:-.}
MODEL=qwen3.5:9b
CAIRNMIND=(python3 -m cairnmind --data-dir "$DATA_DIR")

# Refuse before any model call, failing closed: the installation's data directory (either name), any CAIRNMIND_ or
# DEVBRAIN_ variable (they override config.json and can point at the installation), the installation's ports, and a
# chat role that is not the plain default the native series needs (so that restoring it restores what was there).
python3 - "$DATA_DIR" "$MODEL" <<'PY'
import os, sys
from pathlib import Path
from urllib.parse import urlsplit
from cairnmind import config

data, model = Path(sys.argv[1]).resolve(), sys.argv[2]
installed = [(Path("~/.local/share").expanduser() / name).resolve() for name in config.DATA_DIR_NAMES]
own = sorted(key for key in os.environ if config.is_own_variable(key))
if data == config.default_data_dir() or data in installed or not (data / "config.json").is_file():
    sys.exit(f"{data} is the installation's data directory or has no config.json; use a prepared throwaway directory.")
if own:
    sys.exit(f"Unset {', '.join(own)}: they override config.json and may point at the installation.")
settings = config.Settings.load(data)
engine_port = urlsplit(settings.arcade_connect_url or settings.arcade_url).port
if type(settings.port) is not int or settings.port == 8765 or engine_port in (None, 2480):
    sys.exit("config.json must set its own integer port and an arcade_url with its own port (not 8765, not 2480).")
if settings.memory_chat is not None or settings.chat_model != model or settings.num_ctx != 16384:
    sys.exit(f"The native series needs the default chat role: no memory_chat, chat_model {model}, num_ctx 16384.")
PY

mkdir -p "$REPORT_DIR"
restore() { "${CAIRNMIND[@]}" models set --role chat --provider ollama --model "$MODEL" >/dev/null || \
    echo "WARNING: the chat role was not set back to the local Ollama; run: cairnmind --data-dir $DATA_DIR models set --role chat --provider ollama --model $MODEL" >&2; }

series() {  # series NAME: three runs of each evaluation; a failed run still leaves its output for the table
    for n in 1 2 3; do
        "${CAIRNMIND[@]}" eval run --report-dir "$REPORT_DIR" >"$REPORT_DIR/$1-recall-$n.json" || true
        "${CAIRNMIND[@]}" eval repository "$REPOSITORY" --report-dir "$REPORT_DIR" >"$REPORT_DIR/$1-repository-$n.json" || true
    done
}

series native
"${CAIRNMIND[@]}" models set --role chat --provider openai_compatible --base-url "$V1_URL" --model "$MODEL" \
    --reasoning-effort none --context-window 16384 >"$REPORT_DIR/v1-models-set.json"
trap restore EXIT  # only now: a failed `models set` changed nothing, and the check above makes restoring exact
series v1

python3 - "$REPORT_DIR" <<'PY'
import json, sys
from pathlib import Path
rows = [("path", "run", "passed", "wrong", "calls", "seconds", "cut at length", "max prompt tokens")]
for path in ("native", "v1"):
    for kind in ("recall", "repository"):
        for n in (1, 2, 3):
            try:
                report = json.loads((Path(sys.argv[1]) / f"{path}-{kind}-{n}.json").read_text())
            except (OSError, ValueError):
                rows.append((path, f"{kind} {n}", "no report", "", "", "", "", "")); continue
            if kind == "recall":
                scenarios = report.get("scenarios", [])
                calls = [c for s in scenarios for c in s.get("model_calls", [])]
                prompt = [c["prompt_eval_count"] for c in calls if "prompt_eval_count" in c]
                rows.append((path, f"recall {n}", f"{sum(s['status'] == 'passed' for s in scenarios)} of {report.get('golden_count')}",
                             "", len(calls), report.get("elapsed_seconds"),
                             sum(c.get("candidate", {}).get("done_reason") == "length" for c in calls), max(prompt, default="")))
            else:  # the repository report counts calls per question and keeps no per-call fields
                rows.append((path, f"repository {n}", f"{report.get('complete_and_right')} of {report.get('asked')}",
                             report.get("complete_and_wrong"), sum(q.get("model_calls", 0) for q in report.get("questions", [])),
                             report.get("elapsed_seconds"), "", ""))
for row in rows:
    print(" | ".join(str(cell) for cell in row))
PY
