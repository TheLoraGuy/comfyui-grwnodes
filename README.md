# GR-Downloader

A lightweight ComfyUI custom node panel that lets you pick models/loras from a
JSON manifest and download them straight into the right `models/` subfolder,
using `rclone` (for `b2:` sources) or the Hugging Face CLI (for `hf://` sources).

## Install

1. Drop this whole `GR-Downloader` folder into `custom_nodes/`:
   ```
   ComfyUI/custom_nodes/GR-Downloader/
   ```
2. Restart ComfyUI (or start the pod fresh via your startup.sh — no extra pip
   installs needed, it only uses stdlib + aiohttp, which ComfyUI already has).
3. A purple **"⬇ GR-Downloader"** button appears bottom-right of the ComfyUI
   window. Click it to open the panel.

**Requirements:**
- For `b2:` URLs: `rclone` must be installed and configured with your `b2:`
  remote (as in your `startup.sh` via `B2_KEY_ID` / `B2_APP_KEY`).
- For `hf://` URLs: the Hugging Face CLI (`hf` or `huggingface-cli`) must be
  on `PATH` — typically `pip install -U "huggingface_hub[cli]"`. Optional:
  `pip install hf_transfer` for faster Hub transfers.

If a tool is missing, downloads show an "Error — retry" state with the reason.

## Editing the manifest

Edit `manifest.json` in this folder. Each entry:

```json
{
  "name": "Human-readable label shown in the UI",
  "type": "diffusion",
  "url": "b2:H3-RUNPOD/models/your-file.safetensors"
}
```

`type` controls which ComfyUI subfolder the file lands in, and which tab it
shows up under:

| type                     | Tab    | Destination folder              |
|--------------------------|--------|---------------------------------|
| `diffusion`              | Models | `models/diffusion_models`       |
| `unet`                   | Models | `models/unet`                   |
| `checkpoint`             | Models | `models/checkpoints`            |
| `vae`                    | Models | `models/vae`                    |
| `text_encoders`          | Models | `models/text_encoders`          |
| `upscale_models`         | Models | `models/upscale_models`         |
| `latent_upscaled_models` | Models | `models/latent_upscaled_models` |
| `refmods`                | Models | `models/refmods`                |
| `lora` or `loras`        | Loras  | `models/loras`                  |

Any other `type` is used as the folder name under `models/` (for example
`type: "controlnet"` writes to `models/controlnet`).

`url` can be:
- `b2:bucket/path/to/file.safetensors` (or `b2://...`, both are accepted) —
  downloaded via `rclone copyto` with `--multi-thread-streams 8
  --multi-thread-cutoff 64M`.
- `hf://org/repo/file.safetensors` — downloaded via `hf download ... --local-dir`
  into the matching `models/` subfolder. Example:
  ```json
  {
    "name": "MiniMax H3 Latent Upscaler BF16 Conservative v5",
    "type": "diffusion",
    "url": "hf://Asirus/Minimax-H3-Latent-Upscaler-BF16-MAXQUALITY/minimax_h3_bf16_CONSERVATIVE_v5.safetensors"
  }
  ```
  Equivalent CLI: `hf download hf://Asirus/Minimax-H3-Latent-Upscaler-BF16-MAXQUALITY/minimax_h3_bf16_CONSERVATIVE_v5.safetensors`
- A plain `https://` URL — downloaded via `wget` as a fallback (no
  multi-threading; mainly useful for small misc files).

Changes to `manifest.json` take effect immediately — no restart needed, since
the panel re-fetches it every time you open it or hit Refresh.

## How it works

- `server.py` registers four routes on ComfyUI's own web server:
  `/gr_downloader/manifest`, `/gr_downloader/download`, `/gr_downloader/status`,
  `/gr_downloader/delete`.
- Downloads run as background subprocesses; the panel polls status once a
  second and shows live percent + speed parsed from rclone's own stats output.
- "Installed ✓" is determined by simply checking whether the destination file
  already exists on disk — it does not verify checksums.
- Installed items get a **Delete** button next to them. Clicking it asks for
  confirmation, then removes the file from disk and refreshes the list so you
  can re-download it later if needed. Deleting is blocked while a download for
  that same item is actively running.

## Known limitations

- Only one active job per item name at a time (starting the same download
  twice just no-ops the second click while it's running).
- Delete removes the file only, not the manifest entry — the item stays
  listed so you can re-download it anytime.
- HTTP(S) sources use plain `wget` (no multi-connection). Prefer `hf://` for
  Hub models so transfers go through the HF client / `hf_transfer`.
