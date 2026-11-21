import base64
import binascii
import os
import re
import tempfile
import time
from urllib.parse import urlsplit

import boto3
import requests
import runpod
from botocore.config import Config


COMFY_URL = os.getenv("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
MAX_IMAGE_BYTES = 6 * 1024 * 1024
MAX_VIDEO_BYTES = 100 * 1024 * 1024
IMAGE_TYPES = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


def upload_first_frame(http, workflow, images):
    if not isinstance(images, list) or len(images) != 1:
        raise ValueError("Expected exactly one first frame")
    image = images[0]
    name = image["name"]
    match = re.fullmatch(r"videofly-[0-9a-f-]{36}\.(jpg|png|webp)", name)
    if not match or workflow["13"]["inputs"]["image"] != name:
        raise ValueError("Image name does not match the workflow")
    content_type = IMAGE_TYPES[match.group(1)]
    prefix = f"data:{content_type};base64,"
    data = image["image"]
    if not data.startswith(prefix) or len(data) - len(prefix) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
        raise ValueError("Unsupported first frame")
    try:
        frame = base64.b64decode(data[len(prefix):], validate=True)
    except binascii.Error as error:
        raise ValueError("Invalid first frame") from error
    if not frame or len(frame) > MAX_IMAGE_BYTES:
        raise ValueError("Unsupported first frame size")

    response = http.post(
        f"{COMFY_URL}/upload/image",
        data={"type": "input", "overwrite": "false"},
        files={"image": (name, frame, content_type)},
        timeout=30,
    )
    response.raise_for_status()
    uploaded = response.json()
    if uploaded.get("type") != "input" or uploaded.get("subfolder"):
        raise RuntimeError("ComfyUI returned an unexpected first-frame location")
    uploaded_name = uploaded.get("name", "")
    if not re.fullmatch(r"[\w.-]+", uploaded_name):
        raise RuntimeError("ComfyUI returned an unexpected first-frame name")
    workflow["13"]["inputs"]["image"] = uploaded_name


def run_workflow(http, workflow):
    response = http.post(f"{COMFY_URL}/prompt", json={"prompt": workflow}, timeout=30)
    response.raise_for_status()
    prompt_id = response.json()["prompt_id"]
    deadline = time.monotonic() + int(os.getenv("COMFY_TIMEOUT_SECONDS", "1200"))

    while time.monotonic() < deadline:
        response = http.get(f"{COMFY_URL}/history/{prompt_id}", timeout=20)
        response.raise_for_status()
        job = response.json().get(prompt_id)
        if job:
            status = job.get("status", {})
            if status.get("status_str") == "error":
                raise RuntimeError("ComfyUI execution failed. Check the worker's ComfyUI log.")
            if status.get("completed"):
                for item in job.get("outputs", {}).get("12", {}).get("gifs", []):
                    if item.get("type") == "output" and item.get("filename", "").lower().endswith(".mp4"):
                        return item
                raise RuntimeError("ComfyUI did not produce an MP4 at output node 12")
        time.sleep(2)
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
            output.seek(4)
            if output.read(4) != b"ftyp":
                raise RuntimeError("Invalid MP4 returned by ComfyUI")
            output.seek(0)
            storage.upload_fileobj(output, bucket, key, ExtraArgs={"ContentType": "video/mp4"})
    return storage.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=3600
    )


def handler(job):
    job_id = job["id"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
        raise ValueError("Invalid RunPod job ID")
    workflow = job["input"]["workflow"]
    if workflow["13"]["class_type"] != "LoadImage" or workflow["12"]["class_type"] != "VHS_VideoCombine":
        raise ValueError("Expected the MiniMax H3 first-frame workflow")
    if workflow["12"]["inputs"].get("format") != "video/h264-mp4":
        raise ValueError("The workflow must produce H.264 MP4")
    with requests.Session() as http:
        http.trust_env = False
        upload_first_frame(http, workflow, job["input"]["images"])
        video = run_workflow(http, workflow)
        return {"video_url": store_video(http, video, job_id)}


if __name__ == "__main__":
    location = urlsplit(COMFY_URL)
    if location.scheme != "http" or location.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("COMFY_URL must point to ComfyUI on this worker")
    runpod.serverless.start({"handler": handler})
