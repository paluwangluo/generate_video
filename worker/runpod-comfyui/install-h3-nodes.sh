#!/usr/bin/env bash
set -euo pipefail

nodes_root="${COMFY_ROOT:?COMFY_ROOT is required}/custom_nodes"
python="${COMFY_PYTHON:?COMFY_PYTHON is required}"
mkdir -p "$nodes_root"

install_node() {
  local repository="$1"
  local revision="$2"
  local target="$nodes_root/${repository##*/}"
  git init --quiet "$target"
  git -C "$target" fetch --quiet --depth=1 "https://github.com/$repository.git" "$revision"
  git -C "$target" checkout --quiet --detach FETCH_HEAD
}

install_node T8mars/comfyui-minimax-h3-audio-T8 5ff46c253192e9d8cae185280fd34f4b4add063b
install_node kijai/ComfyUI-KJNodes b8b887cee3bb697c35002a29cc66bd35514b3ebe
install_node Kosinkadink/ComfyUI-VideoHelperSuite 4d907bee61e92c2e65af3bd6383a4e4d356126d1

# KJNodes supplies headless OpenCV. VideoHelperSuite uses the same cv2 module.
"$python" -m pip install --no-cache-dir --break-system-packages \
  --constraint /opt/comfyui-runtime-constraints.txt \
  -r /opt/videofly-worker/requirements.txt \
  -r "$nodes_root/ComfyUI-KJNodes/requirements.txt" \
  imageio-ffmpeg
