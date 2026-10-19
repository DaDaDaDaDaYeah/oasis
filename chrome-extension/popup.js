// chrome.storage.local is the single shared store. With "incognito": "spanning"
// (the default), one extension process serves both normal and incognito windows,
// so anything written here is visible from either — that is what makes the saved
// list identical across a normal tab and an incognito tab.
const STORAGE_KEY = "savedUrls";
const HIDDEN_KEY = "hiddenMode";

const listEl = document.getElementById("list");
const emptyEl = document.getElementById("empty");
const statusEl = document.getElementById("status");
const saveBtn = document.getElementById("save");
const hideBtn = document.getElementById("hide");
const listView = document.getElementById("listView");

// Open eye = list visible (click to hide); closed eye = list hidden.
const EYE_OPEN =
  '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/></svg>';
const EYE_CLOSED =
  '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20C5 20 1 12 1 12a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';

async function getSaved() {
  const data = await chrome.storage.local.get(STORAGE_KEY);
  return Array.isArray(data[STORAGE_KEY]) ? data[STORAGE_KEY] : [];
}

async function setSaved(items) {
  await chrome.storage.local.set({ [STORAGE_KEY]: items });
}

async function isHidden() {
  const data = await chrome.storage.local.get(HIDDEN_KEY);
  return data[HIDDEN_KEY] === true;
}

async function setHidden(value) {
  await chrome.storage.local.set({ [HIDDEN_KEY]: value });
}

function flash(message) {
  statusEl.textContent = message;
  statusEl.hidden = false;
  clearTimeout(flash._t);
  // Error messages here run to a sentence or two; give them time to be read.
  flash._t = setTimeout(() => {
    statusEl.hidden = true;
  }, Math.max(2000, message.length * 120));
}

function formatDate(iso) {
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString();
}

async function render() {
  const hidden = await isHidden();

  // In hidden mode the list is withheld entirely; the eye icon toggles it back.
  // Save stays available so the current tab can still be captured while hidden.
  listView.hidden = hidden;
  hideBtn.innerHTML = hidden ? EYE_CLOSED : EYE_OPEN;
  hideBtn.title = hideBtn.ariaLabel = hidden ? "顯示清單" : "隱藏清單";
  if (hidden) return;

  const items = await getSaved();
  listEl.textContent = "";
  emptyEl.hidden = items.length > 0;

  // Newest first.
  items
    .slice()
    .sort((a, b) => b.createdAt.localeCompare(a.createdAt))
    .forEach((item) => {
      const li = document.createElement("li");
      li.className = "item";

      const main = document.createElement("div");
      main.className = "item-main";

      const a = document.createElement("a");
      a.href = item.url;
      a.textContent = item.title || item.url;
      a.title = item.title || item.url; // hover shows the full title
      a.addEventListener("click", (e) => {
        e.preventDefault();
        openIncognito(item.url);
      });

      const time = document.createElement("time");
      time.dateTime = item.createdAt;
      time.textContent = formatDate(item.createdAt);

      main.append(a, time);

      const remove = document.createElement("button");
      remove.className = "remove";
      remove.type = "button";
      remove.textContent = "刪除";
      remove.addEventListener("click", () => removeItem(item.id));

      li.append(main, remove);
      listEl.append(li);
    });
}

// Saved links always open in incognito. A plain <a> can't target incognito, so
// intercept the click and drive chrome.windows. Reuse an open incognito window
// (add a tab) when there is one; otherwise spawn a fresh incognito window.
async function openIncognito(url) {
  try {
    const wins = await chrome.windows.getAll();
    const incog = wins.find((w) => w.incognito);
    if (incog) {
      await chrome.tabs.create({ windowId: incog.id, url });
      await chrome.windows.update(incog.id, { focused: true });
    } else {
      await chrome.windows.create({ url, incognito: true });
    }
  } catch {
    // Fails when the user hasn't granted incognito access to the extension.
    flash("請在擴充功能設定開啟「允許在無痕模式下執行」。");
  }
}

async function removeItem(id) {
  const items = await getSaved();
  await setSaved(items.filter((item) => item.id !== id));
  render();
}

async function saveCurrent() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.url || /^(chrome|edge|about|chrome-extension):/.test(tab.url)) {
    flash("這個頁面無法儲存。");
    return;
  }

  const items = await getSaved();
  if (items.some((item) => item.url === tab.url)) {
    flash("已經儲存過了。");
    return;
  }

  items.push({
    id: crypto.randomUUID(),
    url: tab.url,
    title: tab.title || "",
    createdAt: new Date().toISOString(),
  });
  await setSaved(items);
  flash("已儲存。");
  render();
}

saveBtn.addEventListener("click", saveCurrent);

hideBtn.addEventListener("click", async () => {
  await setHidden(!(await isHidden()));
  render();
});

// --- Oasis backend + detected media ---------------------------------------
//
// The popup talks to the backend directly. host_permissions <all_urls> exempts
// extension pages from CORS, which is why the backend needs no allow-list entry
// for the extension's origin; it still sends X-Oasis-Client like every Oasis
// client, and the access code when one is set (any backend not on this machine,
// e.g. Docker, requires it).

const BACKEND_KEY = "oasisBackend";
const DEFAULT_BACKEND = "http://localhost:8000";

const settingsEl = document.getElementById("settings");
const backendUrlEl = document.getElementById("backendUrl");
const accessCodeEl = document.getElementById("accessCode");
const mediaView = document.getElementById("mediaView");
const mediaList = document.getElementById("mediaList");

async function getBackend() {
  const data = (await chrome.storage.local.get(BACKEND_KEY))[BACKEND_KEY] || {};
  return { url: (data.url || DEFAULT_BACKEND).replace(/\/+$/, ""), code: data.code || "" };
}

async function backendFetch(path, options = {}) {
  const { url, code } = await getBackend();
  const headers = { "X-Oasis-Client": "1", ...(options.headers || {}) };
  if (code) headers.Authorization = `Bearer ${code}`;
  return fetch(url + path, { ...options, headers });
}

function errorText(status, body) {
  if (body && body.code_required) return "後端未開放遠端存取：請在 Oasis 設定開啟遠端存取，或改用本機後端。";
  if (status === 401) return "需要存取碼：請按右上角齒輪輸入。";
  return (body && body.detail) || `後端回應錯誤 (${status})`;
}

function formatSize(bytes) {
  if (!bytes) return "";
  const mb = bytes / 1024 / 1024;
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb.toFixed(1)} MB`;
}

function displayName(url) {
  try {
    const u = new URL(url);
    const last = decodeURIComponent(u.pathname.slice(u.pathname.lastIndexOf("/") + 1));
    return `${u.hostname} · ${last || u.pathname}`;
  } catch {
    return url;
  }
}

async function currentTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

async function renderMedia() {
  const tab = await currentTab();
  if (!tab) return;
  const k = `media:${tab.id}`;
  const items = (await chrome.storage.session.get(k))[k] || [];
  mediaView.hidden = items.length === 0;
  mediaList.textContent = "";
  // Manifests first: one of them is the whole video, where a progressive hit
  // can still be a preview clip.
  const rank = { hls: 0, dash: 0, http: 1 };
  items
    .slice()
    .sort((a, b) => rank[a.kind] - rank[b.kind] || a.seenAt - b.seenAt)
    .forEach((item) => {
      const li = document.createElement("li");
      li.className = "item media-item";

      const main = document.createElement("div");
      main.className = "item-main";
      const name = document.createElement("div");
      name.className = "media-name";
      name.textContent = displayName(item.url);
      name.title = item.url;
      const meta = document.createElement("div");
      meta.className = "media-meta";
      const kind = document.createElement("span");
      kind.className = "kind";
      kind.textContent = { hls: "HLS", dash: "DASH", http: "MP4" }[item.kind] || item.kind;
      meta.append(kind);
      const size = formatSize(item.size);
      if (size) meta.append(size);
      main.append(name, meta);

      const send = document.createElement("button");
      send.type = "button";
      send.textContent = "下載";
      send.addEventListener("click", () => sendToOasis(item, tab, send));

      li.append(main, send);
      mediaList.append(li);
    });
}

async function sendToOasis(item, tab, button) {
  button.disabled = true;
  try {
    const res = await backendFetch("/api/capture", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        page_url: tab.url,
        title: tab.title || "",
        media_url: item.url,
        kind: item.kind,
        content_type: item.contentType || null,
        // A request with no Referer of its own was made from this page; most
        // hotlink checks want to see it.
        headers: { Referer: tab.url, ...item.headers },
        download: true,
      }),
    });
    let body = null;
    try { body = await res.json(); } catch {}
    if (!res.ok) {
      flash(errorText(res.status, body));
      button.disabled = false;
      return;
    }
    if (body.status === "exists" && !body.download_started) flash("這部影片已在 Oasis 片庫中。");
    else flash("已送到 Oasis，開始下載。");
    button.textContent = "已送出";
  } catch {
    flash("連不到 Oasis 後端：請確認它正在執行，以及齒輪內的網址。");
    button.disabled = false;
  }
}

document.getElementById("settingsBtn").addEventListener("click", async () => {
  settingsEl.hidden = !settingsEl.hidden;
  if (!settingsEl.hidden) {
    const { url, code } = await getBackend();
    backendUrlEl.value = url;
    accessCodeEl.value = code;
  }
});

document.getElementById("saveSettings").addEventListener("click", async () => {
  const url = backendUrlEl.value.trim() || DEFAULT_BACKEND;
  if (!/^https?:\/\//.test(url)) {
    flash("後端網址需以 http:// 或 https:// 開頭。");
    return;
  }
  await chrome.storage.local.set({ [BACKEND_KEY]: { url, code: accessCodeEl.value.trim() } });
  flash("已儲存。");
  settingsEl.hidden = true;
});

document.getElementById("testBackend").addEventListener("click", async () => {
  // Test what is typed, not what is saved, so a typo shows up before saving.
  const url = (backendUrlEl.value.trim() || DEFAULT_BACKEND).replace(/\/+$/, "");
  const code = accessCodeEl.value.trim();
  try {
    const headers = { "X-Oasis-Client": "1" };
    if (code) headers.Authorization = `Bearer ${code}`;
    // /api/health answers everyone and says whether *this* caller is in, which
    // tells "wrong code" apart from "remote access off" in one cheap call.
    const res = await fetch(`${url}/api/health`, { headers });
    let body = null;
    try { body = await res.json(); } catch {}
    if (!res.ok) flash(errorText(res.status, body));
    else if (body.authenticated) flash("連線成功。");
    else if (!body.code_set) flash("後端未開放遠端存取：請在 Oasis 設定開啟遠端存取。");
    else flash(code ? "存取碼不正確。" : "需要存取碼。");
  } catch {
    flash("連不到這個網址。");
  }
});

// Keep the view live if another window (e.g. incognito) changes the store.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && (changes[STORAGE_KEY] || changes[HIDDEN_KEY])) render();
  if (area === "session") renderMedia();
});

render();
renderMedia();
