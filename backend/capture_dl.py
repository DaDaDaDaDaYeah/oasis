"""Download a stream captured by the browser extension (see capture.py).

Runs inside the download worker process in place of download.download() when a
video has a `captures` row. There is no browser and no adapter: the extension
already saw the stream URL and the headers the CDN wants, so this goes straight
to the bytes, through one of three pipelines:

- **hls** — a general HLS client, wider than the adapter path in download.py
  because an arbitrary site may use anything the spec allows: a master playlist
  whose audio is a separate rendition, fMP4 segments with an EXT-X-MAP init
  section, byte-range segments, and AES-128 keys that rotate or derive their IV
  from the media sequence number. SAMPLE-AES and key formats other than plain
  AES-128 are DRM and are refused.
- **dash** — dash.py.
- **http** — httpdl.py (multi-connection, resumable).

Every pipeline works in `movies/<name>/` and moves the finished MP4 up to
`movies/<name>.mp4`, the layout the rest of the backend expects; the worker's
cancel handler wipes that folder through `_active_folder_path`.
"""

import os
import shutil
import subprocess
from urllib.parse import urljoin

import m3u8
import requests

import dash
import httpdl
import segments
from download import movies_dir
from site_config import _clean_filename

_active_folder_path = None

DEFAULT_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')


class CaptureDownloadError(Exception):
    pass


def _media_root() -> str:
    return os.path.dirname(movies_dir())


def target_name(record: dict) -> str:
    """File stem for a captured video: its title, de-collided against what is
    already in movies/ by appending the catalog code (which is unique)."""
    title = _clean_filename(record.get('title_zh_tw') or record.get('title') or '')[:120].strip(' .')
    code = record.get('code') or f'video-{record.get("id")}'
    name = title or code
    movies = movies_dir()
    if os.path.exists(os.path.join(movies, name + '.mp4')) and code not in name:
        name = f'{name} [{code}]'
    return name


def download(record: dict, cap: dict, progress_cb=None) -> str:
    """Fetch the captured stream for `record`. Returns the file's path relative
    to MEDIA_ROOT (what videos.video_path stores)."""
    global _active_folder_path

    def report(pct):
        if progress_cb:
            try:
                progress_cb(int(pct))
            except Exception:
                pass

    report(1)
    headers = {'User-Agent': DEFAULT_UA, **(cap.get('headers') or {})}
    name = target_name(record)
    movies = movies_dir()
    final = os.path.join(movies, name + '.mp4')
    if os.path.exists(final):
        print('影片已存在, 跳過...')
        report(100)
        return os.path.relpath(final, _media_root())

    folder = os.path.join(movies, name)
    os.makedirs(folder, exist_ok=True)
    _active_folder_path = folder
    out = os.path.join(folder, name + '.mp4')
    kind = cap['kind']
    url = cap['media_url']
    print(f'擷取的串流 ({kind}): {url}')

    seg_progress = lambda done, total: report(3 + done / total * 92) if total else None
    report(3)
    if kind == 'http':
        httpdl.download_file(url, out, headers=headers,
                             progress_cb=lambda done, total: report(3 + done / total * 95))
        _check_media(out)
    elif kind == 'dash':
        dash.download(url, folder, out, headers=headers, progress_cb=seg_progress)
    elif kind == 'hls':
        download_hls(url, folder, out, headers=headers, progress_cb=seg_progress, report=report)
    else:
        raise CaptureDownloadError(f'不支援的串流類型: {kind}')

    report(98)
    os.replace(out, final)
    shutil.rmtree(folder, ignore_errors=True)
    _active_folder_path = None
    report(100)
    print(f'✅ 影片已移至: {final}')
    return os.path.relpath(final, _media_root())


def _check_media(path: str) -> None:
    """A captured URL can go stale between capture and download, and some CDNs
    then answer 200 with an HTML error page. Promoting that into movies/ would
    look like a downloaded video, so sniff the first bytes."""
    with open(path, 'rb') as f:
        head = f.read(512).lstrip().lower()
    if head.startswith((b'<!doctype', b'<html', b'<?xml', b'{')):
        os.remove(path)
        raise CaptureDownloadError('伺服器回傳的不是影片（連結可能已過期，請重新擷取）')


def _get(url, headers):
    response = requests.get(url, headers=headers, timeout=20)
    response.raise_for_status()
    return response


def _parse_byterange(value, default_offset=0):
    """'len@offset' (or just 'len') → inclusive (start, end)."""
    if not value:
        return None
    value = str(value).strip('"')
    length, _, offset = value.partition('@')
    start = int(offset) if offset else default_offset
    return start, start + int(length) - 1


def _load_media_playlist(url, headers):
    response = _get(url, headers)
    playlist = m3u8.loads(response.text, uri=response.url)
    return playlist, response.url


def _resolve_master(url, headers):
    """(video_playlist, video_url, audio_url | None) for a master or media URL."""
    response = _get(url, headers)
    base = response.url
    playlist = m3u8.loads(response.text, uri=base)
    if not playlist.playlists:
        return playlist, base, None
    best = max(playlist.playlists, key=lambda p: (p.stream_info.bandwidth or 0))
    res = best.stream_info.resolution
    print(f'HLS: 選擇 {f"{res[0]}x{res[1]}" if res else "最高位元率"}, '
          f'{(best.stream_info.bandwidth or 0) / 1_000_000:.1f} Mbps')
    video_url = urljoin(base, best.uri)
    audio_url = None
    group = best.stream_info.audio
    if group:
        renditions = [m for m in playlist.media if m.type == 'AUDIO' and m.group_id == group and m.uri]
        if renditions:
            pick = next((m for m in renditions if (m.default or '').upper() == 'YES'), renditions[0])
            audio_url = urljoin(base, pick.uri)
    video, video_url = _load_media_playlist(video_url, headers)
    return video, video_url, audio_url


def _plan_track(playlist, base_url, headers, prefix, key_cache):
    """Segment jobs for one media playlist. Returns (jobs, is_fmp4)."""
    if not playlist.segments:
        raise CaptureDownloadError('播放清單內沒有片段')
    if not playlist.is_endlist and (playlist.playlist_type or '').upper() != 'VOD':
        raise CaptureDownloadError('這是直播串流（播放清單沒有結尾），無法下載')

    jobs = []
    seen_init = set()
    fmp4 = False
    next_offset = 0
    seq = playlist.media_sequence or 0
    for i, seg in enumerate(playlist.segments):
        init = getattr(seg, 'init_section', None)
        if init is not None and init.uri:
            fmp4 = True
            init_url = urljoin(base_url, init.uri)
            init_range = _parse_byterange(getattr(init, 'byterange', None))
            key = (init_url, init_range)
            if key not in seen_init:
                # A rotated EXT-X-MAP (rare: a codec switch) is emitted where it
                # appears; the common single-map case puts it once at the front.
                seen_init.add(key)
                jobs.append({'url': init_url, 'name': f'{prefix}{len(jobs):06d}.seg', 'range': init_range})

        job = {'url': urljoin(base_url, seg.uri), 'name': f'{prefix}{len(jobs):06d}.seg'}
        if seg.byterange:
            job['range'] = _parse_byterange(seg.byterange, next_offset)
            next_offset = job['range'][1] + 1
        k = seg.key
        method = (getattr(k, 'method', None) or 'NONE').upper() if k else 'NONE'
        if method not in ('NONE', 'AES-128'):
            raise CaptureDownloadError('此影片受 DRM 保護（SAMPLE-AES），無法下載')
        if method == 'AES-128':
            keyformat = (getattr(k, 'keyformat', None) or 'identity').lower()
            if keyformat != 'identity':
                raise CaptureDownloadError('此影片受 DRM 保護，無法下載')
            key_url = urljoin(base_url, k.uri)
            if key_url not in key_cache:
                key_cache[key_url] = _get(key_url, headers).content[:16]
            job['key'] = key_cache[key_url]
            if k.iv:
                job['iv'] = bytes.fromhex(k.iv[2:] if k.iv.lower().startswith('0x') else k.iv).rjust(16, b'\0')[-16:]
            else:
                # The spec's default IV: the segment's media sequence number.
                job['iv'] = (seq + i).to_bytes(16, 'big')
        jobs.append(job)
    return jobs, fmp4


def download_hls(url, folder, out_path, headers=None, progress_cb=None, report=None):
    video, video_url, audio_url = _resolve_master(url, headers)
    key_cache = {}
    vjobs, vfmp4 = _plan_track(video, video_url, headers, 'v', key_cache)
    ajobs, afmp4 = [], False
    if audio_url:
        audio, audio_url = _load_media_playlist(audio_url, headers)
        ajobs, afmp4 = _plan_track(audio, audio_url, headers, 'a', key_cache)
    print(f'HLS: 影片 {len(vjobs)} 段' + (f'，音訊 {len(ajobs)} 段' if ajobs else ''))

    segments.fetch_all(vjobs + ajobs, folder, headers=headers, progress_cb=progress_cb)
    if report:
        report(96)

    vfile = os.path.join(folder, 'track.video' + ('.mp4' if vfmp4 else '.ts'))
    segments.concat(folder, [j['name'] for j in vjobs], vfile)
    inputs = ['-i', vfile]
    afile = None
    if ajobs:
        afile = os.path.join(folder, 'track.audio' + ('.mp4' if afmp4 else '.ts'))
        segments.concat(folder, [j['name'] for j in ajobs], afile)
        inputs += ['-i', afile]
    _remux(inputs, out_path, bool(afile))

    for j in vjobs + ajobs:
        try:
            os.remove(os.path.join(folder, j['name']))
        except OSError:
            pass
    for f in (vfile, afile):
        if f:
            try:
                os.remove(f)
            except OSError:
                pass


def _remux(inputs, out_path, two_tracks):
    """TS needs the ADTS→ASC bitstream filter to land in MP4; dash.mux covers
    the rest. Retried without it, since the filter errors on non-AAC audio."""
    try:
        dash.mux(inputs + ['-bsf:a', 'aac_adtstoasc'], out_path, two_tracks)
    except IOError:
        dash.mux(inputs, out_path, two_tracks)
