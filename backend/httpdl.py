"""Progressive (non-HLS) file downloader.

The second of the two download pipelines. crawler.py fetches an m3u8's TS
segments across 16 threads and merge.py stitches them; sites that serve one
plain MP4 per video have nothing to stitch, so they come through here instead.

A large file is split into byte ranges fetched over several connections at once
(the technique XDM is built around): a CDN usually caps the throughput of each
connection, not of the client, so one stream leaves most of the line idle. Each
range goes to its own `<dest>.partN` file and a `<dest>.parts.json` records the
split, so an interrupted download resumes every range from where it stopped,
exactly like the single-stream path resumes from a partial file.
"""

import json
import os
import threading
import concurrent.futures

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from crawler import is_stop_requested, reset_stop, wait_stop

CHUNK_SIZE = 1024 * 256

# Parallel connections for one file. Eight is where most CDNs stop scaling and
# start answering 429; OASIS_HTTP_CONNECTIONS=1 restores the old single stream.
CONNECTIONS = max(1, int(os.environ.get('OASIS_HTTP_CONNECTIONS') or 8))
# Below this a split costs more round trips than it saves.
MIN_PART = 4 * 1024 * 1024
PART_ROUNDS = 5


class _RangeUnsupported(Exception):
    """A ranged request came back as anything but 206: the server lied about
    ranges (or changed its mind), so the split cannot be trusted."""


def _make_session(headers: dict | None, pool: int = 1) -> requests.Session:
    session = requests.Session()
    adapter = HTTPAdapter(
        pool_connections=pool,
        pool_maxsize=pool,
        # 429 included: rate-limiting CDNs send it under load, and urllib3
        # honours their Retry-After before retrying.
        max_retries=Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504]),
    )
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    if headers:
        session.headers.update(headers)
    return session


def _content_range_total(response) -> int:
    """Total size from `Content-Range: bytes a-b/total`, or 0 when unknown."""
    value = response.headers.get('Content-Range') or ''
    tail = value.rsplit('/', 1)[-1].strip()
    return int(tail) if tail.isdigit() else 0


def _safe_cb(progress_cb, done, total):
    if not progress_cb or not total:
        return
    try:
        progress_cb(done, total)
    except Exception:
        pass


def _meta_path(dest):
    return dest + '.parts.json'


def _part_path(dest, i):
    return f'{dest}.part{i}'


def _discard_parts(dest):
    meta = _meta_path(dest)
    try:
        with open(meta) as f:
            count = len(json.load(f).get('parts') or [])
    except (OSError, ValueError):
        count = CONNECTIONS
    for i in range(max(count, CONNECTIONS)):
        try:
            os.remove(_part_path(dest, i))
        except OSError:
            pass
    try:
        os.remove(meta)
    except OSError:
        pass


def _plan_parts(total: int) -> list[list[int]]:
    """Inclusive [start, end] byte ranges covering `total`."""
    count = max(1, min(CONNECTIONS, total // MIN_PART))
    size = -(-total // count)
    return [[s, min(s + size, total) - 1] for s in range(0, total, size)]


def download_file(url, dest, headers=None, cookies=None, progress_cb=None):
    """Download `url` to `dest`, resuming from whatever is already there.

    Resume mirrors the segment crawler's "skip what exists" behaviour, so a
    plain shutdown (Ctrl+C, a restart) leaves partial data that the next run
    continues from rather than re-fetching. Returns the total bytes on disk.

    `progress_cb(done_bytes, total_bytes)` is best-effort; a failing sink must
    never abort the download.
    """
    reset_stop()

    if os.path.exists(_meta_path(dest)):
        try:
            return _download_split(url, dest, headers, cookies, progress_cb)
        except _RangeUnsupported:
            print('伺服器不再支援分段下載，改用單一連線...')
            _discard_parts(dest)

    existing = os.path.getsize(dest) if os.path.exists(dest) else 0
    session = _make_session(headers)
    # A fresh download asks for `bytes=0-` rather than nothing: a 206 with a
    # Content-Range is the only answer that proves ranges work, which is what
    # the split below depends on.
    request_headers = {'Range': f'bytes={existing}-'}

    response = session.get(
        url, headers=request_headers, cookies=cookies, stream=True, timeout=30
    )

    if not existing and CONNECTIONS > 1 and response.status_code == 206:
        total = _content_range_total(response)
        if total >= 2 * MIN_PART:
            response.close()
            with open(_meta_path(dest), 'w') as f:
                json.dump({'total': total, 'parts': _plan_parts(total)}, f)
            try:
                return _download_split(url, dest, headers, cookies, progress_cb)
            except _RangeUnsupported:
                print('伺服器不支援分段下載，改用單一連線...')
                _discard_parts(dest)
                response = session.get(url, cookies=cookies, stream=True, timeout=30)

    return _stream_single(response, dest, existing, progress_cb)


def _stream_single(response, dest, existing, progress_cb):
    # A server that ignores the Range header answers 200 with the whole file;
    # appending that to the partial would silently corrupt it, so start over.
    if existing and response.status_code == 200:
        print('伺服器不支援續傳，重新下載...')
        existing = 0
    elif existing and response.status_code == 416:
        # Already have the whole file: the requested range starts past its end.
        response.close()
        _safe_cb(progress_cb, existing, existing)
        return existing
    response.raise_for_status()

    remaining = int(response.headers.get('Content-Length') or 0)
    total = existing + remaining

    size_mb = total / 1024 / 1024 if total else 0
    print(f'開始下載檔案 ({size_mb:.1f} MB)' + (f'，從 {existing / 1024 / 1024:.1f} MB 續傳' if existing else '...'))

    done = existing
    stopped = False
    _safe_cb(progress_cb, done, total)
    mode = 'ab' if existing else 'wb'
    try:
        with open(dest, mode) as f:
            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                if is_stop_requested():
                    stopped = True
                    break
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if total:
                    print(f'\r已下載: {done / 1024 / 1024:.1f} / {size_mb:.1f} MB   ', end='', flush=True)
                _safe_cb(progress_cb, done, total)
    finally:
        response.close()

    print()
    # Raise rather than return short: the caller's next step is to move the file
    # into movies/, and a truncated file promoted there looks downloaded.
    if stopped:
        raise IOError('下載已取消')
    if total and done < total:
        raise IOError(f'下載未完成: 只取得 {done} / {total} bytes')
    return done


def _download_split(url, dest, headers, cookies, progress_cb):
    with open(_meta_path(dest)) as f:
        meta = json.load(f)
    total = int(meta['total'])
    parts = meta['parts']
    session = _make_session(headers, pool=len(parts))
    lock = threading.Lock()

    def have(i):
        try:
            return os.path.getsize(_part_path(dest, i))
        except OSError:
            return 0

    done = {'v': sum(min(have(i), e - s + 1) for i, (s, e) in enumerate(parts))}
    print(f'開始分段下載 ({total / 1024 / 1024:.1f} MB，{len(parts)} 條連線)'
          + (f'，從 {done["v"] / 1024 / 1024:.1f} MB 續傳' if done['v'] else '...'))
    _safe_cb(progress_cb, done['v'], total)

    def fetch(i):
        start, end = parts[i]
        length = end - start + 1
        got = have(i)
        if got > length:
            # More than the range holds can only be a corrupt part: refetch it.
            with lock:
                done['v'] -= length
            os.remove(_part_path(dest, i))
            got = 0
        if got == length:
            return True
        if is_stop_requested():
            return False
        response = session.get(url, headers={'Range': f'bytes={start + got}-{end}'},
                               cookies=cookies, stream=True, timeout=30)
        try:
            if response.status_code != 206:
                if response.status_code in (200, 416):
                    raise _RangeUnsupported()
                response.raise_for_status()
                raise _RangeUnsupported()
            with open(_part_path(dest, i), 'ab') as f:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if is_stop_requested():
                        return False
                    if not chunk:
                        continue
                    # A server that overshoots the requested range must not
                    # bleed bytes into the next part.
                    chunk = chunk[: length - got]
                    f.write(chunk)
                    got += len(chunk)
                    with lock:
                        done['v'] += len(chunk)
                        now = done['v']
                    _safe_cb(progress_cb, now, total)
                    if got >= length:
                        break
        finally:
            response.close()
        return got == length

    pending = list(range(len(parts)))
    for round_num in range(PART_ROUNDS):
        if not pending or is_stop_requested():
            break
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(pending)) as executor:
            futures = {executor.submit(fetch, i): i for i in pending}
            failed = []
            for fut, i in futures.items():
                try:
                    ok = fut.result()
                except _RangeUnsupported:
                    raise
                except Exception as e:
                    if not is_stop_requested():
                        print(f'\n⚠️ 分段 {i} 下載失敗: {e}')
                    ok = False
                if not ok:
                    failed.append(i)
        pending = failed
        if pending and not is_stop_requested():
            print(f'\n{len(pending)} 個分段重試 (round {round_num + 1})')
            wait_stop(min(3 * (round_num + 1), 30))

    print()
    if is_stop_requested():
        raise IOError('下載已取消')
    if pending:
        raise IOError(f'下載未完成: {len(pending)} 個分段失敗')

    # Stitch into a temp name first so a crash mid-copy never leaves a
    # plausible-looking but short file at `dest`.
    tmp = dest + '.joining'
    with open(tmp, 'wb') as out:
        for i in range(len(parts)):
            with open(_part_path(dest, i), 'rb') as f:
                while True:
                    block = f.read(1024 * 1024 * 4)
                    if not block:
                        break
                    out.write(block)
    size = os.path.getsize(tmp)
    if size != total:
        os.remove(tmp)
        _discard_parts(dest)
        raise IOError(f'下載未完成: 只取得 {size} / {total} bytes')
    os.replace(tmp, dest)
    _discard_parts(dest)
    _safe_cb(progress_cb, total, total)
    return total
