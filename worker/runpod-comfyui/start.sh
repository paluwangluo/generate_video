#!/usr/bin/env bash
set -euo pipefail

comfy_root="${COMFY_ROOT:-/comfyui}"
python="${COMFY_PYTHON:-python3}"
export COMFY_URL="http://127.0.0.1:8188"

# ComfyUI 监听容器内的回环地址，由 Worker 调用。
# mmap 保持开启:冷启动时权重按需读进页缓存,不会在启动阶段一次性占满主机内存。
# --disable-mmap 只在长期驻留的热 worker 上划算;端点 idleTimeout 很短时每条任务都是
# 冷启动,强制全量读入 55 GB 反而更容易在加载主模型时被 OOM 杀掉。
comfy_args=(--listen 127.0.0.1 --port 8188 --disable-dynamic-vram)
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

# 启动检查会核对工作流中的节点和模型选择，运行时生成超时由 COMFY_TIMEOUT_SECONDS 控制。
"$python" /opt/videofly-worker/preflight.py \
  --url "$COMFY_URL" \
  --workflow /opt/videofly-worker/minimax-h3-workflow.json \
  --wait-seconds "${COMFY_PREFLIGHT_WAIT_SECONDS:-180}"

"$python" /opt/videofly-worker/handler.py &
worker_pid=$!
wait -n "$comfy_pid" "$worker_pid"
