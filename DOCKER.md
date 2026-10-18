# 🐳 用 Docker 跑 Oasis（家用伺服器 / NAS / 常開電腦）

適合把 Oasis 放在一台一直開著的機器上，手機、Steam Deck、其他電腦只用瀏覽器連進來。

## 啟動

```bash
git clone https://github.com/DaDaDaDaDaYeah/oasis.git
cd oasis
mkdir -p data                 # 影片、資料庫、站台設定都放這裡（先建好，否則 Docker 會建成 root 的）
docker compose up -d --build  # 第一次要下載並編譯，需要幾分鐘
docker compose logs api       # 找到「存取碼： XXXX-XXXX」
```

之後在同一個網路的任何瀏覽器打開 `http://<伺服器IP>:3000`，輸入存取碼即可。

## 重點

- **永遠需要存取碼。** Docker 的連接埠轉發會讓連本機瀏覽器看起來也像「遠端」，所以沒有「本機免密碼」這回事。存取碼在 `docker compose logs api` 裡；存在 `data/oasis.auth.json`，重開不會變。要換新的：刪掉 `data/oasis.auth.json` 後 `docker compose restart api`。
- **資料都在 `./data`**（可用 `OASIS_DATA=/path/to/folder` 改位置）：`oasis.db`、`movies/`、`sites/`（你自己的 adapter 不會被覆蓋，內建的在每次啟動時更新）。備份就是複製這個資料夾。
- **用 `http://<IP>:3000`，不要用公開網站。** https 的公開網站無法連到 http 的伺服器位址（瀏覽器的混合內容限制）。
- **沒有「用本機播放器開啟」。** 那個功能會在後端所在的機器上開播放器，容器內做不到；用網頁內建播放器。
- **封面圖檔**：封面存在 `oasis.db`，同時也會以同名圖檔寫進 `movies/`（`ABC-123.mp4` → `ABC-123.jpg`），既有的圖檔不會被覆蓋。
- **權限**：容器以 UID/GID 1000 執行。你的帳號不是 1000 的話：`PUID=$(id -u) PGID=$(id -g) docker compose up -d`（或寫進 `.env`）。
- **更新**：`git pull && docker compose up -d --build`。軟體內建的更新器只用於打包版，這裡不會用到。
- **換 IP 不用重啟。** 網頁介面預設連到「你瀏覽的那個位址」的 `:8000`，後端也會允許同一個位址的網頁來源（只限 IP、`localhost`、單一名稱或 `.local`，公開網域不會被放行）。要掛在自己的網域／反向代理後面時，用 `ALLOWED_ORIGINS` 加入該網域。

## 安全提醒

- 連接埠 3000 / 8000 對整個區網開放。存取碼是唯一的鎖，且連續輸錯會被鎖定；**不要**直接把這兩個埠轉發到公網。要在外面用，請走 VPN（如 Tailscale）或有驗證的反向代理。
- 容器內的 Chromium 以 `--no-sandbox` 執行（容器本身是隔離層），所以請只用你信任的 adapter。

## 疑難排解

| 現象 | 處理 |
|---|---|
| 打開網頁一直連不上後端 | 防火牆放行 3000、8000；確認用的是 `http://` 且 IP 正確 |
| `docker compose logs api` 找不到存取碼 | 開頭的啟動訊息可能已捲走：`docker compose logs api \| grep 存取碼`，或刪掉 `data/oasis.auth.json` 重啟 |
| 解析／下載時 Chrome 當掉 | 確認 compose 的 `shm_size: 1gb` 沒被移除；`docker compose logs api` 看錯誤 |
| `data/` 寫入被拒 | `ls -ld data`；改用 `PUID`/`PGID`，或 `chown` 該資料夾 |
