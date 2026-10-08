"""
GR-Downloader backend.

Exposes three HTTP routes on ComfyUI's own aiohttp server:

  GET  /grw_downloader/manifest         -> list of items (with "installed" flag)
  POST /grw_downloader/download {name}  -> kicks off a background download job
  GET  /grw_downloader/status?name=...  -> poll progress of a running/finished job

Downloads run via:
  - `rclone copyto` for b2:// sources (8 streams, 64M cutoff)
  - `hf download` for hf:// Hugging Face Hub sources
  - `wget` for plain http(s) URLs
"""

import asyncio
import json
import os
import re
import shutil

from aiohttp import web
from server import PromptServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../custom_nodes/GR-Downloader
COMFY_ROOT = os.path.abspath(os.path.join(BASE_DIR, "..", "..")) # .../ComfyUI
MODELS_DIR = os.path.join(COMFY_ROOT, "models")
MANIFEST_PATH = os.path.join(BASE_DIR, "manifest.json")

# Maps the "type" field in manifest.json to the ComfyUI models subfolder.
# Unknown types use the type string itself (so "vae" -> models/vae) instead of
# silently landing in checkpoints.
TYPE_FOLDERS = {
    "diffusion": "diffusion_models",
    "unet": "unet",
    "checkpoint": "checkpoints",
    "checkpoints": "checkpoints",
    "lora": "loras",
    "loras": "loras",
    "vae": "vae",
    "text_encoder": "text_encoders",
    "text_encoders": "text_encoders",
    "upscale_models": "upscale_models",
    "latent_upscaled_models": "latent_upscaled_models",
    "refmods": "refmods",
}

# In-memory job tracker: { item_name: {status, percent, speed, eta, error} }
JOBS = {}

PERCENT_RE = re.compile(r"(\d+)%")
# rclone: ", 12.3 MiB/s"  |  hf/tqdm: "40.2MB/s" or "40.2 MB/s"
SPEED_RE = re.compile(
    r"(?:,\s*)?([\d.]+\s*[KMGT]?i?B/s)",
    re.IGNORECASE,
)
ETA_RE = re.compile(r"(?:ETA|<\s*)\s*([0-9a-zA-Z:]+)", re.IGNORECASE)


def load_manifest():
    if not os.path.exists(MANIFEST_PATH):
        return []
    with open(MANIFEST_PATH, "r") as f:
        return json.load(f)


def dest_path(item):
    model_type = item.get("type") or "checkpoints"
    folder = TYPE_FOLDERS.get(model_type, model_type)
    filename = item.get("filename") or item["url"].rstrip("/").split("/")[-1]
    return os.path.join(MODELS_DIR, folder, filename)


def parse_hf_url(url):
    """
    Parse hf://[repo_type/]org/repo[/path/to/file] into (repo_type, repo_id, file_path).

    Examples:
      hf://Asirus/Minimax-H3-.../file.safetensors
        -> ("model", "Asirus/Minimax-H3-...", "file.safetensors")
      hf://datasets/google/fleurs/fleurs.py
        -> ("dataset", "google/fleurs", "fleurs.py")
    """
    raw = url
    if raw.startswith("hf://"):
        raw = raw[5:]
    elif raw.startswith("hf:"):
        raw = raw[3:]
    raw = raw.lstrip("/")
    parts = [p for p in raw.split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"Invalid Hugging Face URL (need org/repo): {url}")

    repo_type = "model"
    if parts[0] in ("datasets", "dataset", "spaces", "space", "models", "model"):
        type_map = {
            "datasets": "dataset", "dataset": "dataset",
            "spaces": "space", "space": "space",
            "models": "model", "model": "model",
        }
        repo_type = type_map[parts[0]]
        parts = parts[1:]
        if len(parts) < 2:
            raise ValueError(f"Invalid Hugging Face URL (need org/repo): {url}")

    repo_id = f"{parts[0]}/{parts[1]}"
    file_path = "/".join(parts[2:]) if len(parts) > 2 else None
    return repo_type, repo_id, file_path


def resolve_hf_cli():
    """Prefer `hf`, fall back to legacy `huggingface-cli`."""
    for exe in ("hf", "huggingface-cli"):
        if shutil.which(exe):
            return exe
    return None


def find_downloaded_file(dest_dir, expected_basename):
    """Locate a file after hf --local-dir (may land in a nested subfolder)."""
    direct = os.path.join(dest_dir, expected_basename)
    if os.path.isfile(direct):
        return direct
    for root, _dirs, files in os.walk(dest_dir):
        # Skip HF metadata cache inside local-dir
        if ".cache" in root.split(os.sep):
            continue
        if expected_basename in files:
            return os.path.join(root, expected_basename)
    return None


async def run_download(name, item):
    JOBS[name] = {"status": "downloading", "percent": 0, "speed": "", "eta": "", "error": ""}
    out = dest_path(item)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    url = item["url"]
    post_move_from = None  # set for hf: may need to flatten nested path -> out

    if url.startswith("b2:") or url.startswith("b2://"):
        # Normalize b2:// (as written in chat) down to rclone's expected b2: form.
        rclone_src = url.replace("b2://", "b2:", 1)
        cmd = [
            "rclone", "copyto", rclone_src, out,
            "--multi-thread-streams", "8",
            "--multi-thread-cutoff", "64M",
            "--stats-one-line",
            "--stats", "1s",
        ]
    elif url.startswith("hf://") or url.startswith("hf:"):
        hf_cli = resolve_hf_cli()
        if not hf_cli:
            JOBS[name] = {
                "status": "error", "percent": 0, "speed": "", "eta": "",
                "error": "Hugging Face CLI not found (install: pip install -U 'huggingface_hub[cli]')",
            }
            return
        try:
            repo_type, repo_id, file_path = parse_hf_url(url)
        except ValueError as e:
            JOBS[name] = {
                "status": "error", "percent": 0, "speed": "", "eta": "",
                "error": str(e),
            }
            return

        dest_dir = os.path.dirname(out)
        # Equivalent to: hf download hf://org/repo/file.safetensors --local-dir ...
        # Split form works on both `hf` and older `huggingface-cli`.
        cmd = [hf_cli, "download", repo_id]
        if file_path:
            cmd.append(file_path)
        if repo_type != "model":
            cmd.extend(["--repo-type", repo_type])
        cmd.extend(["--local-dir", dest_dir])
        post_move_from = os.path.basename(out)
    elif url.startswith("http://") or url.startswith("https://"):
        cmd = ["wget", "-q", "--show-progress", "-O", out, url]
    else:
        JOBS[name] = {
            "status": "error", "percent": 0, "speed": "", "eta": "",
            "error": f"Unrecognized URL scheme: {url}",
        }
        return

    try:
        env = os.environ.copy()
        # Faster transfers when hf_transfer is installed (no-op otherwise).
        env.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "1")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        tail = []
        async for raw in proc.stdout:
            line = raw.decode(errors="ignore")
            tail.append(line.rstrip())
            if len(tail) > 20:
                tail.pop(0)
            pm = PERCENT_RE.search(line)
            sm = SPEED_RE.search(line)
            em = ETA_RE.search(line)
            if pm:
                JOBS[name]["percent"] = int(pm.group(1))
            if sm:
                JOBS[name]["speed"] = sm.group(1).strip()
            if em:
                JOBS[name]["eta"] = em.group(1)
        rc = await proc.wait()

        if rc == 0 and post_move_from:
            found = find_downloaded_file(os.path.dirname(out), post_move_from)
            if found and os.path.abspath(found) != os.path.abspath(out):
                os.makedirs(os.path.dirname(out), exist_ok=True)
                shutil.move(found, out)

        if rc == 0 and os.path.exists(out):
            JOBS[name].update(status="done", percent=100)
        else:
            detail = next((t for t in reversed(tail) if t.strip()), "")
            err = f"process exited with code {rc}"
            if detail:
                err = f"{err}: {detail}"
            if rc == 0 and not os.path.exists(out):
                err = f"download finished but file missing at {out}"
            JOBS[name].update(status="error", error=err)
    except FileNotFoundError as e:
        JOBS[name].update(status="error", error=f"command not found: {e}")
    except Exception as e:  # noqa: BLE001 - surface any failure to the UI
        JOBS[name].update(status="error", error=str(e))


def setup_routes():
    routes = PromptServer.instance.routes

    @routes.get("/grw_downloader/manifest")
    async def get_manifest(request):
        items = load_manifest()
        for it in items:
            it["installed"] = os.path.exists(dest_path(it))
        return web.json_response(items)

    @routes.post("/grw_downloader/download")
    async def start_download(request):
        data = await request.json()
        name = data.get("name")
        items = {it["name"]: it for it in load_manifest()}
        if name not in items:
            return web.json_response({"error": "item not found"}, status=404)
        if JOBS.get(name, {}).get("status") == "downloading":
            return web.json_response({"status": "already_running"})
        asyncio.create_task(run_download(name, items[name]))
        return web.json_response({"status": "started"})

    @routes.get("/grw_downloader/status")
    async def status(request):
        name = request.rel_url.query.get("name", "")
        return web.json_response(JOBS.get(name, {"status": "idle", "percent": 0}))

    @routes.post("/grw_downloader/delete")
    async def delete_item(request):
        data = await request.json()
        name = data.get("name")
        items = {it["name"]: it for it in load_manifest()}
        if name not in items:
            return web.json_response({"error": "item not found"}, status=404)
        if JOBS.get(name, {}).get("status") == "downloading":
            return web.json_response({"error": "cannot delete while downloading"}, status=409)

        path = dest_path(items[name])
        try:
            if os.path.exists(path):
                os.remove(path)
            JOBS.pop(name, None)
            return web.json_response({"status": "deleted"})
        except OSError as e:
            return web.json_response({"error": str(e)}, status=500)
