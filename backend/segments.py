"""Parallel, resumable fetch of an ordered list of media segments.

Shared by the DASH pipeline (dash.py) and captured HLS streams (capture_dl.py).
crawler.py predates both and stays as-is for the adapter HLS path; this module
generalises what it does in three ways those sources need: segments may be
byte ranges of one file (DASH SegmentList/SegmentBase), each may carry its own
AES-128 IV (HLS derives the IV from the media sequence number when the playlist
gives none), and a segment is written to a temp name and renamed into place,
so a half-written file never counts as fetched on resume.
"""

import concurrent.futures
import os
import threading

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from crawler import WORKERS, is_stop_requested, wait_stop

ROUNDS = 8


def make_session(headers: dict | None) -> requests.Session:
    session = requests.Session()
    adapter = HTTPAdapter(
        pool_connections=WORKERS,
        pool_maxsize=WORKERS,
        max_retries=Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504]),
    )
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    if headers:
        session.headers.update(headers)
    return session


def fetch_all(jobs: list[dict], folder: str, headers: dict | None = None, progress_cb=None,
              cookies=None) -> None:
    """Fetch every job into `folder/<job['name']>`.

    A job is ``{'url', 'name'}`` plus optional ``'range': (start, end)``
    (inclusive) and ``'key'``/``'iv'`` bytes for AES-128-CBC. Files already on
    disk are skipped, which is the whole resume story. Raises when a segment
    still fails after ROUNDS passes, or on cancel: the caller's next step is a
    merge, and a merge over a gap produces a video that plays and then skips.
    """
    from Crypto.Cipher import AES

    os.makedirs(folder, exist_ok=True)
    session = make_session(headers)
    total = len(jobs)
    lock = threading.Lock()
    remaining = {j['name'] for j in jobs if not os.path.exists(os.path.join(folder, j['name']))}

    def report():
        if not progress_cb or not total:
            return
        try:
            progress_cb(total - len(remaining), total)
        except Exception:
            pass

    report()

    def fetch(job):
        if is_stop_requested():
            return
        path = os.path.join(folder, job['name'])
        req_headers = {}
        if job.get('range'):
            start, end = job['range']
            req_headers['Range'] = f'bytes={start}-{end}'
        try:
            response = session.get(job['url'], headers=req_headers, cookies=cookies, timeout=20)
        except Exception as e:
            if not is_stop_requested():
                print(f'\n⚠️ 片段下載失敗 {job["name"]}: {e}')
            return
        if response.status_code not in (200, 206) or (job.get('range') and response.status_code != 206):
            print(f'\n⚠️ HTTP {response.status_code}: {job["name"]}')
            return
        data = response.content
        if job.get('key'):
            # Fresh cipher per segment: CBC is stateful and must never be
            # shared across threads (same rule as crawler.py).
            data = AES.new(job['key'], AES.MODE_CBC, job['iv']).decrypt(data)
            pad = data[-1] if data else 0
            if 0 < pad <= 16 and data.endswith(bytes([pad]) * pad):
                data = data[:-pad]
        if is_stop_requested():
            return
        tmp = path + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(data)
        os.replace(tmp, path)
        with lock:
            remaining.discard(job['name'])
            # Under the lock so the last report is the final count (see crawler).
            report()

    for round_num in range(ROUNDS):
        pending = [j for j in jobs if j['name'] in remaining]
        if not pending or is_stop_requested():
            break
        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as executor:
            list(executor.map(fetch, pending))
        if remaining and not is_stop_requested():
            print(f'\n{len(remaining)} 個片段重試 (round {round_num + 1})')
            wait_stop(min(3 * (round_num + 1), 30))

    if is_stop_requested():
        raise IOError('下載已取消')
    if remaining:
        raise IOError(f'下載未完成: {len(remaining)} 個片段失敗')


def concat(folder: str, names: list[str], out_path: str) -> None:
    """Join segments in order into `out_path` (via a temp name, so a crash mid
    join never leaves a plausible but short file)."""
    tmp = out_path + '.joining'
    with open(tmp, 'wb') as out:
        for name in names:
            with open(os.path.join(folder, name), 'rb') as f:
                while True:
                    block = f.read(1024 * 1024 * 4)
                    if not block:
                        break
                    out.write(block)
    os.replace(tmp, out_path)
