import { app } from "../../scripts/app.js";

const STYLE = `
#grw-downloader-btn {
  position: fixed; bottom: 80px; right: 16px; z-index: 9999;
  background: #8B5CF6; color: #fff; border: none; padding: 10px 16px;
  border-radius: 8px; cursor: pointer; font-size: 13px;
  box-shadow: 0 2px 8px rgba(0,0,0,.4);
}
#grw-downloader-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,.6); z-index: 10000;
  display: none; align-items: center; justify-content: center;
}
#grw-downloader-modal {
  background: #1e1e1e; color: #eee; width: 640px; max-height: 80vh;
  border-radius: 10px; overflow: hidden; display: flex; flex-direction: column;
  font-family: sans-serif;
}
#grw-downloader-header {
  padding: 14px 18px; background: #151515;
  display: flex; justify-content: space-between; align-items: center;
}
#grw-downloader-tabs { display: flex; gap: 4px; padding: 10px 18px 0; }
.gr-tab {
  padding: 8px 16px; cursor: pointer; border-radius: 6px 6px 0 0;
  background: #2a2a2a; color: #aaa; font-size: 13px;
}
.gr-tab.active { background: #8B5CF6; color: #fff; }
#grw-downloader-list { padding: 12px 18px; overflow-y: auto; flex: 1; }
.gr-item {
  display: flex; align-items: center; justify-content: space-between;
  padding: 10px 0; border-bottom: 1px solid #333; gap: 12px;
}
.gr-item-main { flex: 1; min-width: 0; }
.gr-item-name { font-size: 13px; }
.gr-item-type { font-size: 11px; color: #888; margin-left: 6px; }
.gr-item-size { font-size: 11px; color: #aaa; margin-left: 6px; }
.gr-progress {
  display: none; height: 6px; background: #333; border-radius: 3px;
  margin-top: 6px; overflow: hidden;
}
.gr-item.is-downloading .gr-progress { display: block; }
.gr-progress > span { display: block; height: 100%; width: 0%; background: #8B5CF6; }
.gr-item-note {
  display: none; font-size: 11px; color: #999; margin-top: 4px;
  word-break: break-word;
}
.gr-item.is-downloading .gr-item-note,
.gr-item.is-failed .gr-item-note { display: block; }
.gr-item.is-failed .gr-item-note { color: #ef9a9a; }
.gr-btn {
  background: #8B5CF6; border: none; color: #fff; padding: 6px 12px;
  border-radius: 6px; cursor: pointer; font-size: 12px; min-width: 100px; text-align: center;
}
.gr-btn.done { background: #2e7d32; cursor: default; }
.gr-btn.downloading { background: #555; cursor: default; }
.gr-btn.error { background: #c62828; }
.gr-btn-del {
  background: transparent; border: 1px solid #7a2323; color: #e57373;
  padding: 6px 10px; border-radius: 6px; cursor: pointer; font-size: 12px; margin-left: 6px;
}
.gr-btn-del:hover { background: #7a2323; color: #fff; }
.gr-item-actions { display: flex; align-items: center; }
.gr-group {
  margin: 8px 0 4px; border: 1px solid #333; border-radius: 8px; overflow: hidden;
}
.gr-group-header {
  width: 100%; display: flex; align-items: center; gap: 8px;
  padding: 10px 12px; background: #252525; color: #eee;
  border: none; cursor: pointer; font-size: 13px; font-weight: 600;
  font-family: inherit; text-align: left;
}
.gr-group-header:hover { background: #2c2c2c; }
.gr-group-chevron {
  color: #aaa; width: 12px; display: inline-block; font-size: 10px;
  transition: transform .15s ease;
}
.gr-group.collapsed .gr-group-chevron { transform: rotate(-90deg); }
.gr-group-count {
  margin-left: auto; font-size: 11px; color: #888; font-weight: 400;
}
.gr-group-body { padding: 0 12px 4px; }
.gr-group.collapsed .gr-group-body { display: none; }
.gr-group .gr-item:last-child { border-bottom: none; }
#grw-downloader-close { cursor: pointer; color: #aaa; font-size: 18px; background: none; border: none; }
#grw-downloader-refresh { cursor: pointer; color: #aaa; font-size: 12px; background: none; border: 1px solid #444; border-radius: 6px; padding: 4px 8px; margin-right: 8px; }
`;

let manifestCache = [];
let currentTab = "models";
const pollTimers = {};
// Group names the user has collapsed. Survives Refresh so the panel doesn't pop open again.
const collapsedGroups = new Set();

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function itemGroup(item) {
  return String(item.group || "").trim();
}

const TYPE_LABELS = {
  diffusion: "Diffusion",
  unet: "UNet",
  checkpoint: "Checkpoints",
  checkpoints: "Checkpoints",
  vae: "VAEs",
  text_encoder: "Text encoders",
  text_encoders: "Text encoders",
  upscale_models: "Upscalers",
  latent_upscale_models: "Upscalers",
  latent_upscaled_models: "Upscalers",
  refmods: "Refmods",
  lora: "Loras",
  loras: "Loras",
  h3_adaln: "H3 AdaLN",
  frame_interpolation: "Frame interpolation",
};

function typeLabel(type) {
  const key = String(type || "").trim();
  if (TYPE_LABELS[key]) return TYPE_LABELS[key];
  if (!key) return "Other";
  return key.replace(/_/g, " ");
}

function sectionName(item) {
  const custom = itemGroup(item);
  if (custom) return custom;
  return typeLabel(item.type);
}

function injectStyle() {
  if (document.getElementById("grw-downloader-style")) return;
  const s = document.createElement("style");
  s.id = "grw-downloader-style";
  s.textContent = STYLE;
  document.head.appendChild(s);
}

function buildUI() {
  if (document.getElementById("grw-downloader-btn")) return;

  const btn = document.createElement("button");
  btn.id = "grw-downloader-btn";
  btn.textContent = "⬇ GRW-Downloader";
  document.body.appendChild(btn);

  const overlay = document.createElement("div");
  overlay.id = "grw-downloader-overlay";
  overlay.innerHTML = `
    <div id="grw-downloader-modal">
      <div id="grw-downloader-header">
        <strong>GRW-Downloader</strong>
        <div>
          <button id="grw-downloader-refresh">Refresh</button>
          <button id="grw-downloader-close">✕</button>
        </div>
      </div>
      <div id="grw-downloader-tabs">
        <div class="gr-tab active" data-tab="models">Models</div>
        <div class="gr-tab" data-tab="loras">Loras</div>
      </div>
      <div id="grw-downloader-list"></div>
    </div>
  `;
  document.body.appendChild(overlay);

  btn.onclick = () => {
    overlay.style.display = "flex";
    loadManifest();
  };
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) overlay.style.display = "none";
  });
  overlay.querySelector("#grw-downloader-close").onclick = () => (overlay.style.display = "none");
  overlay.querySelector("#grw-downloader-refresh").onclick = () => loadManifest();

  overlay.querySelectorAll(".gr-tab").forEach((tab) => {
    tab.onclick = () => {
      overlay.querySelectorAll(".gr-tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      currentTab = tab.dataset.tab;
      renderList();
    };
  });
}

async function loadManifest() {
  try {
    const res = await fetch("/grw_downloader/manifest");
    manifestCache = await res.json();
  } catch (e) {
    manifestCache = [];
  }
  renderList();
}

function renderList() {
  const list = document.getElementById("grw-downloader-list");
  if (!list) return;
  list.innerHTML = "";

  const wantLora = currentTab === "loras";
  const items = manifestCache.filter((it) => (it.type === "lora" || it.type === "loras") === wantLora);

  if (items.length === 0) {
    list.innerHTML = `<div style="color:#777;font-size:13px;">No items in this tab yet — add entries to manifest.json.</div>`;
    return;
  }

  const seenGroups = new Set();
  items.forEach((item) => {
    const group = sectionName(item);
    if (seenGroups.has(group)) return;
    seenGroups.add(group);
    const members = items.filter((it) => sectionName(it) === group);
    list.appendChild(renderGroup(group, members));
  });
}

function itemSize(item) {
  const raw = item && item.size;
  if (raw == null || raw === "") return "";
  if (typeof raw === "number" && Number.isFinite(raw) && raw >= 0) {
    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = raw;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    const digits = unit === 0 || value >= 100 ? 0 : 1;
    return `${value.toFixed(digits)} ${units[unit]}`;
  }
  return String(raw).trim();
}

function renderItemRow(item) {
  const job = item.job || {};
  const downloading = job.status === "downloading";
  const failed = job.status === "error" && !item.installed;
  const row = document.createElement("div");
  row.className = "gr-item" + (downloading ? " is-downloading" : "") + (failed ? " is-failed" : "");
  row.dataset.name = item.name;
  const size = itemSize(item);
  const label = item.installed
    ? "Installed ✓"
    : downloading
      ? buttonLabel(job)
      : failed
        ? "Error — retry"
        : "Download";
  row.innerHTML = `
    <div class="gr-item-main">
      <div>
        <span class="gr-item-name">${escapeHtml(item.name)}</span>
        <span class="gr-item-type">${escapeHtml(item.type)}</span>
        ${size ? `<span class="gr-item-size">${escapeHtml(size)}</span>` : ""}
      </div>
      <div class="gr-progress"><span></span></div>
      <div class="gr-item-note"></div>
    </div>
    <div class="gr-item-actions">
      <button class="gr-btn ${item.installed ? "done" : ""} ${downloading ? "downloading" : ""} ${failed ? "error" : ""}">
        ${escapeHtml(label)}
      </button>
      ${item.installed ? `<button class="gr-btn-del" title="Delete file, free up space">Delete</button>` : ""}
    </div>
  `;
  const button = row.querySelector(".gr-btn");
  if (downloading) {
    button.disabled = true;
    applyJob(row, job);
    pollStatus(item.name);
  } else if (failed) {
    button.disabled = false;
    applyJob(row, job);
    button.onclick = () => startDownload(item.name, button);
  } else if (!item.installed) {
    button.onclick = () => startDownload(item.name, button);
  } else {
    button.disabled = true;
  }
  const delBtn = row.querySelector(".gr-btn-del");
  if (delBtn) {
    delBtn.onclick = () => deleteItem(item.name, delBtn, button);
  }
  return row;
}

function buttonLabel(job) {
  const percent = job.percent || 0;
  return `${percent}%${job.speed ? " " + job.speed : ""}`;
}

function findRow(name) {
  const list = document.getElementById("grw-downloader-list");
  if (!list) return null;
  return list.querySelector(`.gr-item[data-name="${CSS.escape(name)}"]`);
}

function applyJob(row, job) {
  if (!row || !job) return;
  const button = row.querySelector(".gr-btn");
  const note = row.querySelector(".gr-item-note");
  const bar = row.querySelector(".gr-progress > span");
  const status = job.status;
  row.classList.toggle("is-downloading", status === "downloading");
  row.classList.toggle("is-failed", status === "error");
  if (status === "downloading") {
    button.disabled = true;
    button.classList.add("downloading");
    button.classList.remove("error", "done");
    button.textContent = buttonLabel(job);
    if (bar) bar.style.width = `${job.percent || 0}%`;
    if (note) note.textContent = job.message || `${job.percent || 0}%`;
  } else if (status === "done") {
    button.textContent = "Installed ✓";
    button.classList.remove("downloading", "error");
    button.classList.add("done");
    button.disabled = true;
    if (bar) bar.style.width = "100%";
    if (note) note.textContent = job.message || "";
  } else if (status === "error") {
    button.textContent = "Error — retry";
    button.title = job.error || "";
    button.classList.remove("downloading", "done");
    button.classList.add("error");
    button.disabled = false;
    if (note) note.textContent = job.error || job.message || "Download failed";
  }
}

function renderGroup(name, members) {
  const key = `${currentTab}::${name}`;
  const installed = members.filter((it) => it.installed).length;
  const section = document.createElement("div");
  section.className = "gr-group" + (collapsedGroups.has(key) ? " collapsed" : "");
  section.innerHTML = `
    <button type="button" class="gr-group-header">
      <span class="gr-group-chevron">▼</span>
      <span class="gr-group-title">${escapeHtml(name)}</span>
      <span class="gr-group-count">${installed}/${members.length} installed</span>
    </button>
    <div class="gr-group-body"></div>
  `;
  const body = section.querySelector(".gr-group-body");
  members.forEach((item) => body.appendChild(renderItemRow(item)));
  section.querySelector(".gr-group-header").onclick = () => {
    const collapsed = section.classList.toggle("collapsed");
    if (collapsed) collapsedGroups.add(key);
    else collapsedGroups.delete(key);
  };
  return section;
}

async function deleteItem(name, delBtn, downloadBtn) {
  if (!confirm(`Delete "${name}" from disk? This frees up space but you'll need to re-download it later.`)) {
    return;
  }
  delBtn.disabled = true;
  delBtn.textContent = "Deleting…";
  try {
    const res = await fetch("/grw_downloader/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const result = await res.json();
    if (!res.ok) {
      alert(`Delete failed: ${result.error || "unknown error"}`);
      delBtn.disabled = false;
      delBtn.textContent = "Delete";
      return;
    }
    // Refresh the whole list so installed-state and buttons stay accurate.
    await loadManifest();
  } catch (e) {
    alert("Delete request failed.");
    delBtn.disabled = false;
    delBtn.textContent = "Delete";
  }
}

async function startDownload(name, btn) {
  applyJob(btn.closest(".gr-item"), { status: "downloading", percent: 0, message: "Starting…" });
  try {
    await fetch("/grw_downloader/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
  } catch (e) {
    btn.textContent = "Request failed";
    btn.classList.add("error");
    btn.disabled = false;
    return;
  }
  pollStatus(name);
}

function pollStatus(name) {
  if (pollTimers[name]) clearInterval(pollTimers[name]);
  pollTimers[name] = setInterval(async () => {
    let s;
    try {
      const res = await fetch(`/grw_downloader/status?name=${encodeURIComponent(name)}`);
      s = await res.json();
    } catch (e) {
      return;
    }
    if (!s.status || s.status === "idle") return;
    const row = findRow(name);
    const button = row && row.querySelector(".gr-btn");
    applyJob(row, s);
    if (s.status === "done") {
      clearInterval(pollTimers[name]);
    } else if (s.status === "error") {
      if (button) button.onclick = () => startDownload(name, button);
      clearInterval(pollTimers[name]);
    }
  }, 1000);
}

app.registerExtension({
  name: "GRW.Downloader",
  async setup() {
    injectStyle();
    buildUI();
  },
});
