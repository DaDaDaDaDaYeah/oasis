# OASIS 瀏覽器擴充功能

一個極簡的 Manifest V3 Chrome 擴充功能，沿用 Oasis 網站的風格（深色底、翡翠綠強
調色、Oasis 貓咪標誌）。點工具列圖示即可看到所有已儲存的網址與其儲存時間；按
**儲存目前分頁** 把目前頁面加進清單。

## 偵測影片並送到 Oasis 下載

參考 XDM 的影片擷取：擴充功能會監看分頁收到的回應，把正在播放的影片（HLS
`.m3u8`、MPEG-DASH `.mpd`、3 MB 以上的 MP4／WebM 等）列在彈出視窗的「此分頁偵測到
的影片」，工具列圖示上的數字是偵測到幾個。按 **下載** 就會連同網頁標題與 CDN 需要
的標頭（Referer、Cookie）送到 Oasis 後端，排進下載佇列。換頁時清單會清空。

第一次用先按右上角的 **齒輪**：

- **後端網址**：後端在同一台電腦就是預設的 `http://localhost:8000`；在 Docker、NAS
  或另一台電腦就填 `http://<那台的 IP>:8000`。
- **存取碼**：只有後端不在這台電腦（或在 Docker 裡）時才需要，在 Oasis 設定 → 遠端
  存取開啟後，於後端視窗／`docker compose logs api` 取得。

小提醒：播放清單常會列出好幾個（主清單、各畫質、音訊），選第一個通常就是主清單，
Oasis 會自動挑最高畫質；受 DRM 保護的影片（例如 Netflix）無法下載。擴充功能只會「看」
瀏覽器本來就收到的回應，不會自己額外去抓影片網址，因為很多網址只能用一次。

## 安裝（未封裝）

1. 開啟 `chrome://extensions`。
2. 打開右上角的 **開發人員模式**。
3. 點 **載入未封裝項目**，選擇這個 `chrome-extension/` 資料夾。

## 與無痕視窗共用資料

清單資料存在 `chrome.storage.local`。因為 `manifest.json` 預設設了
`"incognito": "spanning"`，同一個擴充功能程序同時服務一般視窗與無痕視窗，兩者共用
完全相同的儲存空間 — 在其中一邊儲存，另一邊也會出現。

要讓擴充功能能在無痕模式下執行，必須先授權：

1. 開啟 `chrome://extensions`，找到 **OASIS**，點 **詳細資料**。
2. 開啟 **允許在無痕模式下執行**。

沒開這個開關，Chrome 就不會讓擴充功能在無痕視窗執行。

## 檔案

- `manifest.json` — MV3 設定、權限（`storage`、`tabs`、`webRequest`，以及偵測影片所需的 `<all_urls>`）、spanning 無痕模式。
- `background.js` — 影片偵測（service worker）：分類回應、記下標頭，存在 `chrome.storage.session`（不寫入磁碟）。
- `popup.html` / `popup.css` — 啟用擴充功能時顯示的清單介面。
- `popup.js` — 讀寫 `chrome.storage.local`、渲染清單、儲存／刪除；偵測到的影片清單、後端設定與 `POST /api/capture`。
