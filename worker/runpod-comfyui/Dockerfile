ARG COMFY_BASE_IMAGE=runpod/comfyui@sha256:0c8fb3d7ac331918aa64213217c9094033ac0ee6581cb01aeb6c7bd6853611a1
FROM ${COMFY_BASE_IMAGE}

ARG COMFY_ROOT=/opt/comfyui-baked
ARG COMFY_PYTHON=/usr/bin/python3.12
ENV COMFY_ROOT=${COMFY_ROOT} COMFY_PYTHON=${COMFY_PYTHON}
ENV COMFY_EXTRA_MODEL_PATHS_CONFIG=/opt/videofly-worker/extra-model-paths.yaml
COPY worker/runpod-comfyui/install-h3-nodes.sh worker/runpod-comfyui/requirements.txt /opt/videofly-worker/
RUN bash /opt/videofly-worker/install-h3-nodes.sh
COPY worker/runpod-comfyui/handler.py worker/runpod-comfyui/preflight.py worker/runpod-comfyui/start.sh /opt/videofly-worker/
COPY worker/runpod-comfyui/extra-model-paths.yaml /opt/videofly-worker/
COPY src/ai/providers/minimax-h3-workflow.json /opt/videofly-worker/minimax-h3-workflow.json
ENTRYPOINT ["bash", "/opt/videofly-worker/start.sh"]
CMD []
