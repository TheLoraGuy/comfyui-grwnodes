import { app } from "../../scripts/app.js";

const STYLE = `
#grw-downloader-btn {
  position: fixed; bottom: 16px; right: 16px; z-index: 9999;
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
  padding: 10px 0; border-bottom: 1px solid #333;
}
.gr-item-name { font-size: 13px; }
.gr-item-type { font-size: 11px; color: #888; margin-left: 6px; }
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
#grw-downloader-close { cursor: pointer; color: #aaa; font-size: 18px; background: none; border: none; }
#grw-downloader-refresh { cursor: pointer; color: #aaa; font-size: 12px; background: none; border: 1px solid #444; border-radius: 6px; padding: 4px 8px; margin-right: 8px; }
`;

let manifestCache = [];
let currentTab = "models";
const pollTimers = {};

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
  const items = manifestCache.filter((it) => (it.type === "lora") === wantLora);

  if (items.length === 0) {
    list.innerHTML = `<div style="color:#777;font-size:13px;">No items in this tab yet — add entries to manifest.json.</div>`;
    return;
  }

  items.forEach((item) => {
    const row = document.createElement("div");
    row.className = "gr-item";
    row.innerHTML = `
      <div>
        <span class="gr-item-name">${item.name}</span>
        <span class="gr-item-type">${item.type}</span>
      </div>
      <div class="gr-item-actions">
        <button class="gr-btn ${item.installed ? "done" : ""}" data-name="${item.name}">
          ${item.installed ? "Installed ✓" : "Download"}
        </button>
        ${item.installed ? `<button class="gr-btn-del" title="Delete file, free up space">Delete</button>` : ""}
      </div>
    `;
    const button = row.querySelector(".gr-btn");
    if (!item.installed) {
      button.onclick = () => startDownload(item.name, button);
    } else {
      button.disabled = true;
    }
    const delBtn = row.querySelector(".gr-btn-del");
    if (delBtn) {
      delBtn.onclick = () => deleteItem(item.name, delBtn, button);
    }
    list.appendChild(row);
  });
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
  btn.disabled = true;
  btn.classList.add("downloading");
  btn.textContent = "Starting…";
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
  pollStatus(name, btn);
}

function pollStatus(name, btn) {
  if (pollTimers[name]) clearInterval(pollTimers[name]);
  pollTimers[name] = setInterval(async () => {
    let s;
    try {
      const res = await fetch(`/grw_downloader/status?name=${encodeURIComponent(name)}`);
      s = await res.json();
    } catch (e) {
      return;
    }
    if (s.status === "downloading") {
      btn.textContent = `${s.percent || 0}%${s.speed ? " " + s.speed : ""}`;
    } else if (s.status === "done") {
      btn.textContent = "Installed ✓";
      btn.classList.remove("downloading");
      btn.classList.add("done");
      btn.disabled = true;
      clearInterval(pollTimers[name]);
    } else if (s.status === "error") {
      btn.textContent = "Error — retry";
      btn.title = s.error || "";
      btn.classList.remove("downloading");
      btn.classList.add("error");
      btn.disabled = false;
      btn.onclick = () => startDownload(name, btn);
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
