"""Media captured by the browser extension from any site — no adapter needed.

This is the XDM model: instead of Oasis scraping a page it knows (site_config
adapters), the extension watches what the user's own browser fetches while a
video plays, and hands over the stream URL it saw — an m3u8, an mpd or a plain
MP4 — together with the headers the CDN expects (Referer, Cookie, ...). The
backend files a catalog entry for it and queues the download; capture_dl.py
then fetches the stream without ever opening a browser.

Two things are load-bearing here:

- **Headers are allow-listed.** The extension forwards what the page sent so a
  hotlink-protected CDN accepts us, but only the few headers that decide that.
  Everything else (Host, Range, Content-Length, ...) would break or forge the
  request.
- **Remote callers cannot point the backend at the private network.** The
  backend fetches whatever URL it is handed and the result is then streamable
  back through /api/stream, so an authorised remote device could otherwise
  read the router's admin page, or anything else only this machine can reach.
  A caller at this machine can already reach those itself, so it is exempt.
"""

import ipaddress
import json
import re
import socket
from urllib.parse import urlsplit

import catalog

KINDS = ('hls', 'dash', 'http')

# Headers worth replaying to the CDN: the ones hotlink protection and signed
# URLs actually look at. Compared lower-case.
_ALLOWED_HEADERS = {
    'referer': 'Referer',
    'origin': 'Origin',
    'user-agent': 'User-Agent',
    'cookie': 'Cookie',
    'accept-language': 'Accept-Language',
}
_MAX_HEADER_LEN = 8192


class CaptureError(ValueError):
    pass


def detect_kind(media_url: str, hint: str | None = None, content_type: str | None = None) -> str:
    """hls / dash / http from the extension's hint, the Content-Type, or the URL."""
    hint = (hint or '').strip().lower()
    if hint in KINDS:
        return hint
    ct = (content_type or '').lower()
    if 'mpegurl' in ct:
        return 'hls'
    if 'dash+xml' in ct:
        return 'dash'
    path = urlsplit(media_url).path.lower()
    if path.endswith('.m3u8') or path.endswith('.m3u'):
        return 'hls'
    if path.endswith('.mpd'):
        return 'dash'
    return 'http'


def clean_headers(headers) -> dict:
    out = {}
    if not isinstance(headers, dict):
        return out
    for key, value in headers.items():
        name = _ALLOWED_HEADERS.get(str(key).strip().lower())
        if not name or not isinstance(value, str):
            continue
        value = value.strip()
        # A CR/LF in a value would let it inject further headers.
        if not value or len(value) > _MAX_HEADER_LEN or re.search(r'[\r\n]', value):
            continue
        out[name] = value
    return out


def _is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if getattr(addr, 'ipv4_mapped', None):
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


def check_destination(url: str, allow_private: bool) -> None:
    """Refuse non-http(s) URLs always, and private/loopback hosts for remote callers."""
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise CaptureError('只接受 http/https 的影片網址')
    if allow_private:
        return
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise CaptureError(f'無法解析主機: {parts.hostname}')
    if not infos or not all(_is_public(info[4][0]) for info in infos):
        raise CaptureError('遠端裝置不能讓後端下載內網位址的內容')


def get(video_id: int) -> dict | None:
    conn = catalog.get_connection()
    try:
        row = conn.execute("SELECT * FROM captures WHERE video_id = ?", (video_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    cap = dict(row)
    try:
        cap['headers'] = json.loads(cap.get('headers') or '{}')
    except ValueError:
        cap['headers'] = {}
    return cap


def _by_media_url(media_url: str) -> int | None:
    conn = catalog.get_connection()
    try:
        row = conn.execute("SELECT video_id FROM captures WHERE media_url = ?", (media_url,)).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _store(video_id, page_url, media_url, kind, headers):
    conn = catalog.get_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO captures (video_id, page_url, media_url, kind, headers) "
            "VALUES (?, ?, ?, ?, ?)",
            (video_id, page_url, media_url, kind, json.dumps(headers)),
        )
        conn.commit()
    finally:
        conn.close()


def delete(video_id: int) -> None:
    conn = catalog.get_connection()
    try:
        conn.execute("DELETE FROM captures WHERE video_id = ?", (video_id,))
        conn.commit()
    finally:
        conn.close()


def _fallback_title(page_url: str, media_url: str) -> str:
    host = urlsplit(page_url).hostname or urlsplit(media_url).hostname or 'video'
    return host


def create(page_url: str, media_url: str, title: str | None = None, kind: str | None = None,
           headers=None, cover: str | None = None, content_type: str | None = None,
           allow_private: bool = False) -> tuple[dict, bool]:
    """File a captured stream. Returns ``(record, created)``.

    Idempotent on the stream URL: the extension may send the same capture twice
    (a double click, a retry), and that must re-use the entry rather than file a
    second copy of the video. A title carrying a code that is already in the
    catalog (a JAV site the adapters don't cover) re-uses that entry too, since
    `code` is UNIQUE and the manual-add path would refuse it.
    """
    media_url = (media_url or '').strip()
    page_url = (page_url or '').strip() or media_url
    if not media_url:
        raise CaptureError('影片網址不可為空')
    check_destination(media_url, allow_private)
    kind = detect_kind(media_url, kind, content_type)
    headers = clean_headers(headers)

    existing = _by_media_url(media_url)
    if existing is not None:
        record = catalog.get_video_by_id(existing)
        if record:
            _store(existing, page_url, media_url, kind, headers)  # refresh expiring tokens
            return record, False
        delete(existing)

    title = (title or '').strip() or _fallback_title(page_url, media_url)
    code = catalog.extract_code(title)
    if code:
        same = catalog.search_by_code(code)
        if same:
            record = same[0] if isinstance(same, list) else same
            _store(record['id'], page_url, media_url, kind, headers)
            return record, False

    record = catalog.create_manual_video(url=page_url, title=title, cover=(cover or '').strip() or None)
    _store(record['id'], page_url, media_url, kind, headers)
    return record, True
