import base64
import binascii
import os
import re
import tempfile
import time
import uuid
from urllib.parse import urlsplit

import boto3
import requests
import runpod
from botocore.config import Config


# 容器内 ComfyUI 的地址及输入图、成片的大小上限。
COMFY_URL = os.getenv("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
MAX_IMAGE_BYTES = 6 * 1024 * 1024
MAX_VIDEO_BYTES = 100 * 1024 * 1024
IMAGE_TYPES = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
JOB_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")

_STARTED = time.monotonic()


def log(stage, **fields):
    # 统一的相对时间标记，用来看清每个阶段实际花了多久。
    parts = [f"[videofly] t+{time.monotonic() - _STARTED:7.1f}s", stage]
    parts.extend(f"{key}={value}" for key, value in fields.items())
    print(" ".join(parts), flush=True)


def detect_image_type(frame):
    # 按文件头判断真实格式，不信任调用方给的文件名和 MIME。
    if frame.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if frame.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if len(frame) >= 12 and frame[:4] == b"RIFF" and frame[8:12] == b"WEBP":
        return "webp"
    return None


def upload_first_frame(http, workflow, images):
    if not isinstance(images, list) or len(images) != 1:
        raise ValueError("Expected exactly one first frame")
    image = images[0]
    data = image.get("image")
    if not isinstance(data, str) or not data.startswith("data:"):
        raise ValueError("Unsupported first frame")
    header, _, payload = data.partition(",")
    if not header.endswith(";base64") or len(payload) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
        raise ValueError("Unsupported first frame")
    try:
        frame = base64.b64decode(payload, validate=True)
    except binascii.Error as error:
        raise ValueError("Invalid first frame") from error
    if not frame or len(frame) > MAX_IMAGE_BYTES:
        raise ValueError("Unsupported first frame size")

    kind = detect_image_type(frame)
    if kind is None:
        raise ValueError("Unsupported first frame format")

    # 调用方给的文件名一律作废，统一改成服务端生成的名字。
    # 这样既消除了路径穿越的可能，调用方也不必再遵守命名规则。
    name = f"videofly-{uuid.uuid4()}.{kind}"
    log("first_frame", bytes=len(frame), kind=kind, sent_as=image.get("name", "-"))

    response = http.post(
        f"{COMFY_URL}/upload/image",
        data={"type": "input", "overwrite": "false"},
        files={"image": (name, frame, IMAGE_TYPES[kind])},
        timeout=30,
    )
    response.raise_for_status()
    uploaded = response.json()
    if uploaded.get("type") != "input" or uploaded.get("subfolder"):
        raise RuntimeError("ComfyUI returned an unexpected first-frame location")
    uploaded_name = uploaded.get("name", "")
    if not re.fullmatch(r"[\w.-]+", uploaded_name):
        raise RuntimeError("ComfyUI returned an unexpected first-frame name")
    # ComfyUI 可能重命名上传文件，节点 13 要引用最终保存的名称。
    if uploaded_name != name:
        log("first_frame_renamed", by_comfyui=uploaded_name)
    workflow["13"]["inputs"]["image"] = uploaded_name


def queue_state(http, prompt_id):
    # /queue 每项是 [number, prompt_id, prompt, extra_data, outputs_to_execute]。
    try:
        data = http.get(f"{COMFY_URL}/queue", timeout=10).json()
    except (requests.RequestException, ValueError):
        return None
    for key, state in (("queue_running", "running"), ("queue_pending", "pending")):
        for item in data.get(key, []):
            if isinstance(item, list) and len(item) > 1 and item[1] == prompt_id:
                return state
    return None


def log_comfy_timings(status):
    # ComfyUI 在 history.status.messages 里给出各阶段的毫秒时间戳。
    stamps = {}
    for message in status.get("messages", []):
        if isinstance(message, list) and len(message) == 2 and isinstance(message[1], dict):
            stamp = message[1].get("timestamp")
            if isinstance(stamp, int):
                stamps[message[0]] = stamp
    start = stamps.get("execution_start")
    success = stamps.get("execution_success")
    cached = stamps.get("execution_cached")
    if isinstance(start, int) and isinstance(success, int):
        log("comfy_timings", exec_s=round((success - start) / 1000, 1))
    elif isinstance(start, int):
        log("comfy_timings", exec_s="unfinished")
    if isinstance(start, int) and isinstance(cached, int):
        log("comfy_cached_after_s", round((cached - start) / 1000, 1))


def run_workflow(http, workflow):
    timeout_seconds = int(os.getenv("COMFY_TIMEOUT_SECONDS", "1200"))
    log("comfy_submit", comfy_timeout_s=timeout_seconds)
    response = http.post(f"{COMFY_URL}/prompt", json={"prompt": workflow}, timeout=30)
    response.raise_for_status()
    accepted = response.json()
    prompt_id = accepted["prompt_id"]
    log("comfy_accepted", prompt_id=prompt_id, queue_number=accepted.get("number", "-"))

    # 这是 ComfyUI 推理的等待时间，RunPod 的执行超时另在 Endpoint 里设置。
    deadline = time.monotonic() + timeout_seconds
    last_state = None
    while time.monotonic() < deadline:
        state = queue_state(http, prompt_id)
        if state != last_state:
            last_state = state
            log("comfy_queue", state=state or "unknown")
        response = http.get(f"{COMFY_URL}/history/{prompt_id}", timeout=20)
        response.raise_for_status()
        job = response.json().get(prompt_id)
        if job:
            status = job.get("status", {})
            if status.get("status_str") == "error":
                raise RuntimeError("ComfyUI execution failed. Check the worker's ComfyUI log.")
            if status.get("completed"):
                log_comfy_timings(status)
                # 节点 12 负责合成成片，VideoHelperSuite 在 gifs 字段里返回 MP4 文件信息。
                for item in job.get("outputs", {}).get("12", {}).get("gifs", []):
                    if item.get("type") == "output" and item.get("filename", "").lower().endswith(".mp4"):
                        return item
                raise RuntimeError("ComfyUI did not produce an MP4 at output node 12")
        time.sleep(2)
    log("comfy_timeout", waited_s=timeout_seconds)
    raise TimeoutError("ComfyUI generation exceeded its execution timeout")


def store_video(http, video, job_id):
    bucket = os.environ["WORKER_S3_BUCKET"]
    storage = boto3.client(
        "s3",
        endpoint_url=os.environ["WORKER_S3_ENDPOINT"],
        region_name=os.getenv("WORKER_S3_REGION", "auto"),
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=10,
            read_timeout=30,
        ),
    )
    # 将成片存到私有桶，再返回有效期为一小时的签名下载链接。
    key = f"runpod-results/{job_id}.mp4"
    params = {field: video.get(field, "") for field in ("filename", "subfolder", "type")}
    with http.get(f"{COMFY_URL}/view", params=params, stream=True, timeout=(10, 60)) as response:
        response.raise_for_status()
        if int(response.headers.get("content-length", "0")) > MAX_VIDEO_BYTES:
            raise RuntimeError("ComfyUI result exceeds the maximum video size")
        with tempfile.TemporaryFile() as output:
            size = 0
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                size += len(chunk)
                if size > MAX_VIDEO_BYTES:
                    raise RuntimeError("ComfyUI result exceeds the maximum video size")
                output.write(chunk)
            log("video_fetched", bytes=size)
            output.seek(4)
            if output.read(4) != b"ftyp":
                raise RuntimeError("Invalid MP4 returned by ComfyUI")
            output.seek(0)
            storage.upload_fileobj(output, bucket, key, ExtraArgs={"ContentType": "video/mp4"})
    log("video_stored", bucket=bucket, key=key, bytes=size)
    return storage.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=3600
    )


def handler(job):
    job_id = job["id"]
    if not JOB_ID_PATTERN.fullmatch(job_id):
        raise ValueError("Invalid RunPod job ID")
    workflow = job["input"]["workflow"]
    for node_id, expected in (("13", "LoadImage"), ("12", "VHS_VideoCombine")):
        if workflow.get(node_id, {}).get("class_type") != expected:
            raise ValueError(f"Expected the MiniMax H3 first-frame workflow (node {node_id} must be {expected})")
    if workflow["12"]["inputs"].get("format") != "video/h264-mp4":
        raise ValueError("The workflow must produce H.264 MP4")
    log("job_start", job_id=job_id)
    with requests.Session() as http:
        # 容器内调用不经过环境变量配置的代理。
        http.trust_env = False
        upload_first_frame(http, workflow, job["input"]["images"])
        video = run_workflow(http, workflow)
        url = store_video(http, video, job_id)
    log("job_done", job_id=job_id)
    return {"video_url": url}


if __name__ == "__main__":
    location = urlsplit(COMFY_URL)
    if location.scheme != "http" or location.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("COMFY_URL must point to ComfyUI on this worker")
    log(
        "worker_ready",
        comfy_url=COMFY_URL,
        comfy_timeout_s=os.getenv("COMFY_TIMEOUT_SECONDS", "1200"),
    )
    runpod.serverless.start({"handler": handler})
