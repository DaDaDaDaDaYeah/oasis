// Video sniffer, after XDM's request watcher. Every response the browser
// receives is checked for a playable stream — an HLS playlist, a DASH manifest
// or a progressive video file — and what is found is listed per tab, so the
// popup can hand it to the Oasis backend (POST /api/capture). This is what lets
// Oasis download from a site it has no adapter for: the page has already done
// the hard part (token exchange, player JS) and we only replay the request.
//
// Nothing here fetches anything. A manifest URL is often signed for one use, so
// probing it ourselves (to tell a master playlist from a variant, say) could
// burn the token the player — or the backend — still needs.

const PER_TAB_MAX = 30;
// Progressive files below this are thumbnails, previews and ad pings, and fMP4
// segments served with a video/* type look exactly like them.
const MIN_HTTP_BYTES = 3 * 1024 * 1024;

const EXT_KIND = {
  m3u8: "hls", m3u: "hls", mpd: "dash",
  mp4: "http", m4v: "http", webm: "http", mov: "http", mkv: "http", flv: "http",
};
// Stream pieces, never offered: the manifest that lists them is.
const SEGMENT_EXT = new Set(["ts", "m4s", "m4a", "aac", "vtt", "key", "init"]);
// The headers the backend will replay (it allow-lists them again).
const KEEP_HEADERS = new Set(["referer", "origin", "user-agent", "cookie", "accept-language"]);

const requestHeaders = new Map();   // requestId -> {name: value}

function extOf(url) {
  try {
    const path = new URL(url).pathname;
    const last = path.slice(path.lastIndexOf("/") + 1);
    const dot = last.lastIndexOf(".");
    return dot > 0 ? last.slice(dot + 1).toLowerCase() : "";
  } catch {
    return "";
  }
}

function header(headers, name) {
  const h = (headers || []).find((x) => x.name.toLowerCase() === name);
  return h ? h.value || "" : "";
}

function classify(url, contentType) {
  const ct = (contentType || "").toLowerCase().split(";")[0].trim();
  const ext = extOf(url);
  if (SEGMENT_EXT.has(ext)) return null;
  if (ct.includes("mpegurl")) return "hls";
  if (ct.includes("dash+xml")) return "dash";
  if (ct === "video/mp2t" || ct.includes("iso.segment")) return null;
  if (ct.startsWith("video/")) return "http";
  if (EXT_KIND[ext]) {
    // An extension match with a type that is plainly not media (an HTML error
    // page at a .mp4 URL) is not a video.
    if (ct.startsWith("text/html") || ct.startsWith("image/") || ct.includes("json")) return null;
    return EXT_KIND[ext];
  }
  return null;
}

function sizeOf(headers) {
  const range = header(headers, "content-range");
  const total = range.split("/")[1];
  if (total && /^\d+$/.test(total)) return Number(total);
  const len = header(headers, "content-length");
  return /^\d+$/.test(len) ? Number(len) : 0;
}

// storage.session survives the service worker being stopped (MV3 kills it
// when idle) but not the browser session, and is never written to disk.
const key = (tabId) => `media:${tabId}`;
let chain = Promise.resolve();
function serialised(fn) {
  chain = chain.then(fn, fn);
  return chain;
}

async function setBadge(tabId, count) {
  try {
    await chrome.action.setBadgeBackgroundColor({ tabId, color: "#10b981" });
    await chrome.action.setBadgeText({ tabId, text: count ? String(count) : "" });
  } catch {
    // The tab is gone.
  }
}

function record(tabId, item) {
  return serialised(async () => {
    const k = key(tabId);
    const list = (await chrome.storage.session.get(k))[k] || [];
    const existing = list.find((x) => x.url === item.url);
    if (existing) {
      // Keep the freshest headers: signed cookies rotate while a video plays.
      Object.assign(existing, { headers: item.headers, size: item.size || existing.size, seenAt: item.seenAt });
    } else {
      list.push(item);
      if (list.length > PER_TAB_MAX) list.shift();
    }
    await chrome.storage.session.set({ [k]: list });
    await setBadge(tabId, list.length);
  });
}

function clearTab(tabId) {
  return serialised(async () => {
    await chrome.storage.session.remove(key(tabId));
    await setBadge(tabId, 0);
  });
}

chrome.webRequest.onSendHeaders.addListener(
  (details) => {
    if (details.tabId < 0) return;
    const kept = {};
    for (const h of details.requestHeaders || []) {
      if (KEEP_HEADERS.has(h.name.toLowerCase()) && h.value) kept[h.name] = h.value;
    }
    requestHeaders.set(details.requestId, kept);
    if (requestHeaders.size > 2000) requestHeaders.delete(requestHeaders.keys().next().value);
  },
  { urls: ["<all_urls>"] },
  // extraHeaders is what makes Chrome expose Cookie and Referer here.
  ["requestHeaders", "extraHeaders"],
);

chrome.webRequest.onHeadersReceived.addListener(
  (details) => {
    const sent = requestHeaders.get(details.requestId);
    requestHeaders.delete(details.requestId);
    if (details.tabId < 0) return;
    // A failed response (an expired token's 403/410) would only fail again.
    if (details.statusCode < 200 || details.statusCode >= 300) return;
    if ((details.initiator || "").startsWith("chrome-extension://")) return;
    // Oasis's own player must not list the library's videos back to itself.
    if (/\/api\/stream\//.test(details.url)) return;

    const contentType = header(details.responseHeaders, "content-type");
    const kind = classify(details.url, contentType);
    if (!kind) return;
    const size = sizeOf(details.responseHeaders);
    if (kind === "http" && size && size < MIN_HTTP_BYTES) return;

    record(details.tabId, {
      url: details.url,
      kind,
      contentType,
      size,
      headers: sent || {},
      seenAt: Date.now(),
    });
  },
  { urls: ["<all_urls>"], types: ["xmlhttprequest", "media", "other", "object", "sub_frame"] },
  ["responseHeaders"],
);

// A new page in the tab: what the last one played is no longer on screen.
chrome.webRequest.onBeforeRequest.addListener(
  (details) => {
    if (details.tabId >= 0) clearTab(details.tabId);
  },
  { urls: ["<all_urls>"], types: ["main_frame"] },
);

chrome.tabs.onRemoved.addListener((tabId) => {
  serialised(() => chrome.storage.session.remove(key(tabId)));
});
