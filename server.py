"""
GR-Downloader backend.

Exposes three HTTP routes on ComfyUI's own aiohttp server:

  GET  /grw_downloader/manifest         -> list of items (with "installed" flag)
  POST /grw_downloader/download {name}  -> kicks off a background download job
  GET  /grw_downloader/status?name=...  -> poll progress of a running/finished job

Downloads run via:
  - `rclone copyto` for b2:// sources (8 streams, 64M cutoff)
  - Hugging Face Hub (`hf_hub_download`, or `hf download` if the library is missing) for hf:// sources
  - `wget` for plain http(s) URLs
"""

import asyncio
import json
import os
import re
import shutil
import time

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

# In-memory job tracker: { item_name: {status, percent, speed, eta, error, message, dest} }
JOBS = {}

PERCENT_RE = re.compile(r"(\d+)%")
# rclone: ", 12.3 MiB/s"  |  hf/tqdm: "40.2MB/s" or "40.2 MB/s"
SPEED_RE = re.compile(
    r"(?:,\s*)?([\d.]+\s*[KMGT]?i?B/s)",
    re.IGNORECASE,
)
ETA_RE = re.compile(r"(?:ETA|<\s*)\s*([0-9a-zA-Z:]+)", re.IGNORECASE)
# "1.2 GiB / 10 GiB" or "1.2GiB/10GiB" — used when a tool omits the percent.
SIZE_PAIR_RE = re.compile(
    r"([\d.]+)\s*([KMGT]?i?B)\s*/\s*([\d.]+)\s*([KMGT]?i?B)",
    re.IGNORECASE,
)
PASTED_HF_CLI_RE = re.compile(
    r"^(?:hf|huggingface-cli)\s+download\s+(hf://\S+)",
    re.IGNORECASE,
)
_SIZE_UNITS = {
    "b": 1,
    "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12,
    "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4,
}


def _size_bytes(number, unit):
    key = unit.lower().replace("bytes", "b").replace("byte", "b")
    if key.endswith("ib") or key in ("kb", "mb", "gb", "tb"):
        return float(number) * _SIZE_UNITS.get(key, 1)
    return float(number) * _SIZE_UNITS.get(key, 1)


def push_output(pending, chunk):
    """Split a stdout chunk on \\n or \\r. Returns (new_pending, complete_lines).

    rclone, wget, and Hugging Face tqdm rewrite one line with carriage returns,
    so a newline-only reader stays at 0% until the process exits.
    """
    text = pending + chunk.replace("\r\n", "\n").replace("\r", "\n")
    parts = text.split("\n")
    pending = parts.pop()
    lines = [part for part in parts if part.strip()]
    return pending, lines


def parse_progress(line):
    """Return (percent or None, speed, eta) from one tool progress line."""
    percent = None
    pm = PERCENT_RE.search(line)
    if pm:
        percent = int(pm.group(1))
    else:
        pair = SIZE_PAIR_RE.search(line)
        if pair:
            done = _size_bytes(pair.group(1), pair.group(2))
            total = _size_bytes(pair.group(3), pair.group(4))
            if total > 0:
                percent = max(0, min(99, int(done * 100 / total)))
    speed_match = SPEED_RE.search(line)
    eta_match = ETA_RE.search(line)
    speed = speed_match.group(1).strip() if speed_match else ""
    eta = eta_match.group(1) if eta_match else ""
    return percent, speed, eta


def normalize_url(url):
    """Turn a pasted `hf download hf://...` command into an hf:// URL."""
    text = (url or "").strip()
    match = PASTED_HF_CLI_RE.match(text)
    if match:
        return match.group(1), True
    return text, False


HF_TOKEN_NAMES = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACE_HUB_TOKEN")


def parse_env_assignment(line):
    """Parse one KEY=VALUE line. Vast writes /etc/environment as HF_TOKEN='...'."""
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    if text.startswith("export "):
        text = text[7:].strip()
    if "=" not in text:
        return None
    key, value = text.split("=", 1)
    key = key.strip()
    if not key:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    return key, value


def read_env_file(path):
    found = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                parsed = parse_env_assignment(line)
                if parsed:
                    found[parsed[0]] = parsed[1]
    except OSError:
        return {}
    return found


# Anything shorter, or one of these placeholders, is not a user access token.
# The Hub answers those with the "unauthenticated requests" warning.
_TOKEN_PLACEHOLDERS = {
    "hf_token",
    "hugging_face_hub_token",
    "huggingface_hub_token",
    "none",
    "null",
    "undefined",
    "changeme",
    "your_token",
    "token",
    "placeholder",
}


def clean_hf_token(value):
    """Strip quotes, a pasted assignment, and a leading Bearer prefix.

    Vast and some startup scripts leave the quotes from /etc/environment in the
    process value (`'hf_...'`). The Hub then treats the request as anonymous.
    """
    token = (value or "").replace("\ufeff", "").strip()
    while len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        token = token[1:-1].strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if "=" in token:
        key, rest = token.split("=", 1)
        if key.strip() in HF_TOKEN_NAMES:
            token = rest.strip()
            while len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
                token = token[1:-1].strip()
    return token


def token_usable(token):
    if not token or len(token) < 20 or any(ch.isspace() for ch in token):
        return False
    if token.lower() in _TOKEN_PLACEHOLDERS or token.startswith("${") or token.startswith("<"):
        return False
    return re.fullmatch(r"[A-Za-z0-9_\-]+", token) is not None


def token_shape(token):
    """Non-secret description. Never include the token itself."""
    starts = "yes" if token.startswith("hf_") else "no"
    return f"length {len(token)}, starts with hf_: {starts}"


def _token_rank(token):
    if not token_usable(token):
        return 0
    if token.startswith("hf_"):
        return 2
    return 1


def _collect_tokens(candidates, mapping, source_for):
    if not mapping:
        return
    for name in HF_TOKEN_NAMES:
        raw = mapping.get(name)
        if raw is None:
            continue
        cleaned = clean_hf_token(raw)
        if not cleaned:
            continue
        candidates.append((cleaned, source_for(name), cleaned != str(raw).strip()))


def read_proc_environ(pid):
    """Read a process environment. Docker injects HF_TOKEN into PID 1."""
    try:
        with open(f"/proc/{pid}/environ", "rb") as handle:
            raw = handle.read()
    except OSError:
        return {}
    found = {}
    for item in raw.split(b"\0"):
        if not item or b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        try:
            found[key.decode()] = value.decode()
        except UnicodeDecodeError:
            continue
    return found


def huggingface_token():
    """Find a real Hub token from this process, PID 1, Vast's env file, or the token file.

    RunPod injects the secret into the container environment. Vast writes template
    variables to /etc/environment and into PID 1. ComfyUI often does not inherit
    either. A non-empty value is not enough: quotes, a Bearer prefix, or a
    placeholder still make the Hub answer as if no token was sent.

    Returns (token or None, source or None, detail). Detail never contains the token.
    """
    candidates = []
    _collect_tokens(candidates, os.environ, lambda name: f"process environment {name}")

    pids = []
    for pid in (os.getpid(), os.getppid(), 1):
        if pid and pid not in pids:
            pids.append(pid)
    proc_envs = []
    for pid in pids:
        env_map = read_proc_environ(pid)
        if not env_map:
            continue
        proc_envs.append(env_map)
        _collect_tokens(candidates, env_map, lambda name, pid=pid: f"/proc/{pid}/environ ({name})")

    workspace = os.environ.get("WORKSPACE") or "/workspace"
    env_files = [
        "/etc/environment",
        os.path.join(workspace, ".env"),
        os.path.expanduser("~/.env"),
        "/root/.env",
    ]
    env_dir = "/etc/environment.d"
    try:
        extra = sorted(os.listdir(env_dir))
    except OSError:
        extra = []
    env_files.extend(os.path.join(env_dir, name) for name in extra)
    for path in env_files:
        _collect_tokens(candidates, read_env_file(path), lambda name, path=path: f"{path} ({name})")

    homes = []
    for env_map in (os.environ, *proc_envs):
        hf_home = (env_map.get("HF_HOME") or "").strip()
        if hf_home:
            homes.append(hf_home)
    homes.extend([
        os.path.join(workspace, ".hf_home"),
        os.path.join(os.path.expanduser("~"), ".cache", "huggingface"),
        "/root/.cache/huggingface",
        os.path.join(os.path.expanduser("~"), ".huggingface"),
    ])
    seen = set()
    for home in homes:
        if home in seen:
            continue
        seen.add(home)
        path = os.path.join(home, "token")
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                raw = handle.read()
        except OSError:
            continue
        cleaned = clean_hf_token(raw)
        if cleaned:
            candidates.append((cleaned, path, cleaned != raw.strip()))

    best = None
    best_rank = 0
    for item in candidates:
        rank = _token_rank(item[0])
        if rank > best_rank:
            best = item
            best_rank = rank
    if best is None:
        if candidates:
            token, source, _dirty = candidates[0]
            return None, None, (
                f"Hugging Face auth: value from {source} is not a Hub token "
                f"({token_shape(token)}). A user token starts with hf_."
            )
        return None, None, (
            "Hugging Face auth: no HF_TOKEN in this process, PID 1, "
            "/etc/environment, the workspace .env, or the HF token file"
        )

    token, source, dirty = best
    bits = [token_shape(token)]
    if dirty:
        bits.append("quotes or a Bearer prefix removed")
    if candidates[0][1] != source and _token_rank(candidates[0][0]) < best_rank:
        bits.append(f"ignored unusable value in {candidates[0][1]}")
    return token, source, ", ".join(bits)


def apply_hf_token(env):
    """Copy a usable token into an environment mapping.

    Returns (token or None, log line). The download also passes this string as
    the explicit Hub token. A process variable alone is not sent when
    HF_HUB_DISABLE_IMPLICIT_TOKEN is set, and a quoted value is rejected by the Hub.
    """
    token, source, detail = huggingface_token()
    env["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "0"
    if not token:
        return None, detail
    env["HF_TOKEN"] = token
    env["HUGGING_FACE_HUB_TOKEN"] = token
    return token, f"Hugging Face auth: using token from {source} ({detail})"


def enable_implicit_hf_token():
    """The disable flag is read once, at import, and then ignores the environment."""
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "0"
    try:
        import huggingface_hub.constants as hf_constants
        hf_constants.HF_HUB_DISABLE_IMPLICIT_TOKEN = False
    except Exception:
        pass


def install_hf_token():
    """Put the token on this process so ComfyUI's own Hub calls are authenticated."""
    token, note = apply_hf_token(os.environ)
    enable_implicit_hf_token()
    print(f"[GRW] {note}", flush=True)
    return token


def console(name, message, store=True):
    """Print one line. ComfyUI's Logs panel mirrors stdout/stderr."""
    print(f"[GRW] {name}: {message}", flush=True)
    if not store:
        return
    job = JOBS.get(name)
    if job is not None:
        job["message"] = message


def fail_job(name, err):
    JOBS[name].update(status="error", error=err, message=err)
    console(name, f"FAILED {err}")


def job_view(name):
    job = JOBS.get(name)
    if not job:
        return None
    return {
        "status": job.get("status", "idle"),
        "percent": job.get("percent", 0),
        "speed": job.get("speed", ""),
        "eta": job.get("eta", ""),
        "error": job.get("error", ""),
        "message": job.get("message", ""),
        "dest": job.get("dest", ""),
    }


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


async def iter_output_lines(stream):
    pending = ""
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            if pending.strip():
                yield pending
            return
        pending, lines = push_output(pending, chunk.decode(errors="ignore"))
        for line in lines:
            yield line


def progress_message(percent, speed, eta):
    parts = [f"{percent}%"]
    if speed:
        parts.append(speed)
    if eta:
        parts.append(f"ETA {eta}")
    return " · ".join(parts)


def _format_rate(rate):
    if not rate or rate < 0:
        return ""
    units = ("B/s", "KB/s", "MB/s", "GB/s")
    value = float(rate)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f}{unit}"
        value /= 1024
    return ""


def _job_tqdm(name):
    from tqdm.auto import tqdm

    class GRWTqdm(tqdm):
        def update(self, n=1):
            displayed = super().update(n)
            job = JOBS.get(name)
            total = self.total or 0
            if job is None or job.get("status") != "downloading" or not total:
                return displayed
            percent = max(0, min(99, int((self.n or 0) * 100 / total)))
            job["percent"] = percent
            speed = _format_rate((self.format_dict or {}).get("rate"))
            if speed:
                job["speed"] = speed
            job["message"] = progress_message(percent, job.get("speed", ""), job.get("eta", ""))
            now = time.monotonic()
            bucket = percent // 5
            if bucket != job.get("_bucket", -1) or now - job.get("_console_at", 0) >= 15:
                console(name, job["message"])
                job["_bucket"] = bucket
                job["_console_at"] = now
            return displayed

    return GRWTqdm


def _hf_download_blocking(name, repo_id, file_path, repo_type, dest_dir, token):
    """Download on a worker thread with an explicit token string."""
    enable_implicit_hf_token()
    tqdm_cls = _job_tqdm(name)
    if file_path:
        from huggingface_hub import hf_hub_download
        return hf_hub_download(
            repo_id=repo_id,
            filename=file_path,
            repo_type=repo_type,
            local_dir=dest_dir,
            token=token,
            tqdm_class=tqdm_cls,
        )
    from huggingface_hub import snapshot_download
    return snapshot_download(
        repo_id=repo_id,
        repo_type=repo_type,
        local_dir=dest_dir,
        token=token,
        tqdm_class=tqdm_cls,
    )


def _finish_download(name, out, rc, tail, post_move_from):
    if rc == 0 and post_move_from:
        found = find_downloaded_file(os.path.dirname(out), post_move_from)
        if found and os.path.abspath(found) != os.path.abspath(out):
            os.makedirs(os.path.dirname(out), exist_ok=True)
            shutil.move(found, out)

    if rc == 0 and os.path.exists(out):
        JOBS[name].update(status="done", percent=100, message="Installed")
        console(name, f"100% installed at {out}")
        return
    detail = next((t for t in reversed(tail) if t.strip()), "")
    err = f"process exited with code {rc}"
    if detail:
        err = f"{err}: {detail}"
    if rc == 0 and not os.path.exists(out):
        err = f"download finished but file missing at {out}"
    fail_job(name, err)


async def run_download(name, item):
    JOBS[name] = {
        "status": "downloading",
        "percent": 0,
        "speed": "",
        "eta": "",
        "error": "",
        "message": "Starting…",
        "dest": "",
    }
    out = dest_path(item)
    JOBS[name]["dest"] = out
    os.makedirs(os.path.dirname(out), exist_ok=True)
    url, rewritten = normalize_url(item.get("url") or "")
    post_move_from = None  # set for hf: may need to flatten nested path -> out

    if rewritten:
        console(name, f"rewrote pasted CLI command to {url}")

    hf_token = None
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
        try:
            repo_type, repo_id, file_path = parse_hf_url(url)
        except ValueError as e:
            fail_job(name, str(e))
            return

        dest_dir = os.path.dirname(out)
        post_move_from = os.path.basename(out)
        hf_token, note = apply_hf_token(os.environ)
        enable_implicit_hf_token()
        console(name, note)
        console(name, f"started -> {out}")
        try:
            await asyncio.to_thread(
                _hf_download_blocking, name, repo_id, file_path, repo_type, dest_dir, hf_token,
            )
        except ImportError:
            hf_cli = resolve_hf_cli()
            if not hf_cli:
                fail_job(name, "Hugging Face library not found (install: pip install -U 'huggingface_hub[cli]')")
                return
            cmd = [hf_cli, "download", repo_id]
            if file_path:
                cmd.append(file_path)
            if repo_type != "model":
                cmd.extend(["--repo-type", repo_type])
            cmd.extend(["--local-dir", dest_dir])
            if hf_token:
                cmd.extend(["--token", hf_token])
        except Exception as e:  # noqa: BLE001 - surface Hub failures to the UI
            fail_job(name, f"Hugging Face download failed: {e}")
            return
        else:
            _finish_download(name, out, 0, [], post_move_from)
            return
    elif url.startswith("http://") or url.startswith("https://"):
        cmd = ["wget", "-q", "--show-progress", "-O", out, url]
    else:
        fail_job(name, f"Unrecognized URL scheme: {url}")
        return

    try:
        env = os.environ.copy()
        if url.startswith("hf://") or url.startswith("hf:"):
            apply_hf_token(env)
            # Older Hub builds switch to hf_transfer when this is 1, and that
            # path has sent requests the Hub counts as anonymous.
            env.pop("HF_HUB_ENABLE_HF_TRANSFER", None)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        tail = []
        last_bucket = -1
        last_console = 0.0
        async for line in iter_output_lines(proc.stdout):
            line = line.strip()
            if not line:
                continue
            tail.append(line)
            if len(tail) > 20:
                tail.pop(0)
            lowered = line.lower()
            # The Hub prints this when a request had no usable Authorization.
            # With a token attached, keep the percent note instead of this line.
            if "unauthenticated requests" in lowered and hf_token:
                if not JOBS[name].get("_anon_logged"):
                    console(
                        name,
                        "Hub reported an unauthenticated request. A token was still attached to this download.",
                        store=False,
                    )
                    JOBS[name]["_anon_logged"] = True
                continue
            percent, speed, eta = parse_progress(line)
            if percent is not None:
                JOBS[name]["percent"] = percent
            if speed:
                JOBS[name]["speed"] = speed
            if eta:
                JOBS[name]["eta"] = eta
            shown = JOBS[name]["percent"]
            if percent is None:
                message = line[-180:]
            else:
                message = progress_message(shown, JOBS[name]["speed"], JOBS[name]["eta"])
            JOBS[name]["message"] = message
            now = time.monotonic()
            bucket = shown // 5
            looks_like_error = any(
                word in lowered
                for word in ("error", "traceback", "unauthorized", "unauthenticated", "denied", "not found", "401", "403", "404")
            )
            due = last_console == 0.0 or now - last_console >= 15
            if looks_like_error or (percent is not None and (bucket != last_bucket or due)) or (percent is None and due and message != JOBS[name].get("_logged")):
                console(name, message)
                JOBS[name]["_logged"] = message
                last_bucket = bucket
                last_console = now
        rc = await proc.wait()
        _finish_download(name, out, rc, tail, post_move_from)
    except FileNotFoundError as e:
        fail_job(name, f"command not found: {e}")
    except Exception as e:  # noqa: BLE001 - surface any failure to the UI
        fail_job(name, str(e))


def setup_routes():
    install_hf_token()
    routes = PromptServer.instance.routes

    @routes.get("/grw_downloader/manifest")
    async def get_manifest(request):
        items = load_manifest()
        for it in items:
            it["installed"] = os.path.exists(dest_path(it))
            job = job_view(it.get("name", ""))
            if job:
                it["job"] = job
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
        return web.json_response(job_view(name) or {"status": "idle", "percent": 0})

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
