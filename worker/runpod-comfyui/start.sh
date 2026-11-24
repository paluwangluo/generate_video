#!/usr/bin/env bash
set -euo pipefail

comfy_root="${COMFY_ROOT:-/comfyui}"
python="${COMFY_PYTHON:-python3}"
export COMFY_URL="http://127.0.0.1:8188"

log() { printf '[videofly] %s %s\n' "$(date -u +%H:%M:%S)" "$*"; }

# 权重默认直接从卷读:每换一个 worker 都要重读约 52 GB(实测约 190 秒)。
# 把权重一次性拷到本地盘后,同一 worker 上的后续任务读本地 NVMe,加载时间大幅下降。
# 拷贝只在容器启动时做一次,失败则回退到卷路径,不影响出片。
volume_models="${H3_VOLUME_MODELS:-/runpod-volume/models}"
local_models="${H3_LOCAL_MODELS:-/local-models}"
cache_models="${H3_CACHE_MODELS:-1}"

model_config="${COMFY_EXTRA_MODEL_PATHS_CONFIG:-}"

if [[ "$cache_models" == "1" && -d "$volume_models" ]]; then
  if [[ -f "$local_models/.ready" ]]; then
    log "model cache hit: $local_models"
  else
    log "copying models: $volume_models -> $local_models"
    started=$(date +%s)
    if rm -rf "$local_models" \
      && mkdir -p "$local_models" \
      && cp -a "$volume_models/." "$local_models/" \
      && touch "$local_models/.ready"; then
      log "model cache ready in $(( $(date +%s) - started ))s"
    else
      log "model copy failed after $(( $(date +%s) - started ))s, falling back to volume"
      rm -rf "$local_models"
      cache_models=0
    fi
  fi
fi

if [[ "$cache_models" == "1" && -f "$local_models/.ready" ]]; then
  # 运行期改写模型路径,指向本地副本。
  runtime_config="/tmp/videofly-extra-model-paths.yaml"
  cat > "$runtime_config" <<EOF
h3_models:
  base_path: $local_models
  diffusion_models: diffusion_models
  text_encoders: text_encoders
  vae: vae
EOF
  model_config="$runtime_config"
fi

# ComfyUI 监听容器内的回环地址，由 Worker 调用。
# mmap 保持开启:冷启动时权重按需读进页缓存,不会在启动阶段一次性占满主机内存。
# --disable-mmap 只在长期驻留的热 worker 上划算;端点 idleTimeout 很短时每条任务都是
# 冷启动,强制全量读入 55 GB 反而更容易在加载主模型时被 OOM 杀掉。
comfy_args=(--listen 127.0.0.1 --port 8188 --disable-dynamic-vram)
if [[ -n "$model_config" ]]; then
  comfy_args+=(--extra-model-paths-config "$model_config")
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

# 启动检查会核对工作流中的节点和模型选择,运行时生成超时由 COMFY_TIMEOUT_SECONDS 控制。
"$python" /opt/videofly-worker/preflight.py \
  --url "$COMFY_URL" \
  --workflow /opt/videofly-worker/minimax-h3-workflow.json \
  --wait-seconds "${COMFY_PREFLIGHT_WAIT_SECONDS:-180}"

"$python" /opt/videofly-worker/handler.py &
worker_pid=$!
wait -n "$comfy_pid" "$worker_pid"
