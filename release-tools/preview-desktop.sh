#!/usr/bin/env bash
# Keep the requested synthetic preview visible if attaching reports an error.
set -u
preview_python=$1
preview_data=$2
"$preview_python" -m devbrain.terminal_preview --data-dir "$preview_data" --attach-only
preview_exit=$?
if (( preview_exit != 0 )); then
    printf '\nPreview attachment returned %s. Press Enter to close.\n' "$preview_exit"
    read -r _
fi
exit "$preview_exit"
