#!/usr/bin/env bash
set -euo pipefail

comfy_root="${COMFY_ROOT:-/comfyui}"
python="${COMFY_PYTHON:-python3}"
export COMFY_URL="http://127.0.0.1:8188"

comfy_args=(--listen 127.0.0.1 --port 8188)
if [[ -n "${COMFY_EXTRA_MODEL_PATHS_CONFIG:-}" ]]; then
  comfy_args+=(--extra-model-paths-config "$COMFY_EXTRA_MODEL_PATHS_CONFIG")
fi
cd "$comfy_root"
"$python" main.py "${comfy_args[@]}" "$@" &
comfy_pid=$!
worker_pid=
cleanup() {
  kill "$comfy_pid" ${worker_pid:+"$worker_pid"} 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

"$python" /opt/videofly-worker/preflight.py \
  --url "$COMFY_URL" \
  --workflow /opt/videofly-worker/minimax-h3-workflow.json \
  --wait-seconds "${COMFY_PREFLIGHT_WAIT_SECONDS:-180}"

"$python" /opt/videofly-worker/handler.py &
worker_pid=$!
wait -n "$comfy_pid" "$worker_pid"
