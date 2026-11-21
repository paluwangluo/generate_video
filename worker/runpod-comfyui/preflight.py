import argparse
import json
from pathlib import Path
import time
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener


def check_workflow(graph, nodes):
    problems = []
    for node_id, node in graph.items():
        class_name = node["class_type"]
        schema = nodes.get(class_name)
        if not schema:
            problems.append(f"Node {node_id}: {class_name} is not registered")
            continue
        fields = schema.get("input", {})
        inputs = {**fields.get("required", {}), **fields.get("optional", {})}
        for field, value in node["inputs"].items():
            if field == "image" or not isinstance(value, str):
                continue
            definition = inputs.get(field)
            if not definition:
                continue
            options = definition[0]
            if options == "COMBO" and len(definition) > 1:
                options = definition[1].get("options")
            if isinstance(options, list) and value not in options:
                problems.append(f"Node {node_id} ({class_name}): {field}={value!r} is unavailable")
    return problems


def load_node_definitions(url, wait_seconds):
    location = urlsplit(url)
    if location.scheme != "http" or location.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("The ComfyUI preflight URL must use loopback HTTP")
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            with opener.open(url.rstrip("/") + "/object_info", timeout=10) as response:
                return json.load(response)
        except OSError as error:
            if time.monotonic() >= deadline:
                raise SystemExit(f"ComfyUI node definitions are unavailable: {error}") from error
            time.sleep(2)


def main():
    parser = argparse.ArgumentParser(description="Check registered ComfyUI nodes and model choices for an API workflow")
    parser.add_argument("--url", default="http://127.0.0.1:8188")
    parser.add_argument("--workflow", type=Path, required=True)
    parser.add_argument("--wait-seconds", type=int, default=0)
    args = parser.parse_args()

    graph = json.loads(args.workflow.read_text(encoding="utf-8"))
    problems = check_workflow(graph, load_node_definitions(args.url, args.wait_seconds))
    if problems:
        raise SystemExit("ComfyUI workflow preflight failed:\n" + "\n".join(problems))
    print(f"ComfyUI workflow preflight passed for all {len(graph)} nodes")


if __name__ == "__main__":
    main()
