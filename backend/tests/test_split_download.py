"""httpdl's multi-connection path against a real local HTTP server.

What matters is the same as for the single stream: the stitched file must be
byte-identical, a partial must resume per range rather than restart, a server
that stops honouring ranges must fall back rather than corrupt, and a cancel or
short range must raise instead of leaving a promotable file.
"""

import http.server
import os
import re
import threading

import pytest

import crawler
import httpdl

PAYLOAD = os.urandom(1024 * 300)


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    payload = PAYLOAD
    honour_ranges = True
    short_ranges = False
    seen = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        rng = self.headers.get('Range')
        type(self).seen.append(rng)
        data = self.payload
        m = re.match(r'bytes=(\d+)-(\d*)', rng or '')
        if not m or not self.honour_ranges:
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else len(data) - 1
        body = data[start:end + 1]
        self.send_response(206)
        self.send_header('Content-Range', f'bytes {start}-{end}/{len(data)}')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if self.short_ranges and start > 0:
            body = body[: len(body) // 2]
            self.close_connection = True
        self.wfile.write(body)


@pytest.fixture
def server():
    handler = type('H', (_RangeHandler,), {'seen': []})
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    t = threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
    t.start()
    handler.url = f'http://127.0.0.1:{httpd.server_address[1]}/v.mp4'
    yield handler
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture(autouse=True)
def _small_parts(monkeypatch):
    # Shrink the split threshold so a 300 KB payload is split four ways.
    monkeypatch.setattr(httpdl, 'MIN_PART', 1024 * 64)
    monkeypatch.setattr(httpdl, 'CONNECTIONS', 4)
    monkeypatch.setattr(crawler, 'wait_stop', lambda s: False)
    monkeypatch.setattr(httpdl, 'wait_stop', lambda s: False)
    crawler.reset_stop()
    yield
    crawler.reset_stop()


def _leftovers(tmp_path):
    return sorted(p.name for p in tmp_path.iterdir() if p.name != 'out.mp4')


def test_split_download_is_byte_identical_and_cleans_up(server, tmp_path):
    dest = tmp_path / 'out.mp4'
    calls = []
    assert httpdl.download_file(server.url, str(dest), progress_cb=lambda d, t: calls.append(d)) == len(PAYLOAD)
    assert dest.read_bytes() == PAYLOAD
    assert _leftovers(tmp_path) == []
    # Four disjoint ranges were requested after the probe.
    assert len([r for r in server.seen[1:] if r and r.endswith(tuple('0123456789'))]) == 4
    assert calls[-1] == len(PAYLOAD)


def test_split_resumes_each_range_from_its_part_file(server, tmp_path):
    dest = tmp_path / 'out.mp4'
    httpdl.download_file(server.url, str(dest))  # learn the plan
    plan = httpdl._plan_parts(len(PAYLOAD))
    dest.unlink()
    # Recreate an interrupted state: plan on disk, each part half fetched.
    import json
    (tmp_path / 'out.mp4.parts.json').write_text(json.dumps({'total': len(PAYLOAD), 'parts': plan}))
    for i, (s, e) in enumerate(plan):
        (tmp_path / f'out.mp4.part{i}').write_bytes(PAYLOAD[s:s + (e - s + 1) // 2])
    server.seen.clear()
    httpdl.download_file(server.url, str(dest))
    assert dest.read_bytes() == PAYLOAD
    for i, (s, e) in enumerate(plan):
        assert f'bytes={s + (e - s + 1) // 2}-{e}' in server.seen


def test_server_that_stops_honouring_ranges_falls_back_to_one_stream(server, tmp_path):
    dest = tmp_path / 'out.mp4'
    import json
    plan = httpdl._plan_parts(len(PAYLOAD))
    (tmp_path / 'out.mp4.parts.json').write_text(json.dumps({'total': len(PAYLOAD), 'parts': plan}))
    (tmp_path / 'out.mp4.part0').write_bytes(b'junk')
    server.honour_ranges = False
    httpdl.download_file(server.url, str(dest))
    assert dest.read_bytes() == PAYLOAD
    assert _leftovers(tmp_path) == []


def test_short_ranges_raise_and_leave_no_file_at_dest(server, tmp_path):
    server.short_ranges = True
    dest = tmp_path / 'out.mp4'
    with pytest.raises(IOError):
        httpdl.download_file(server.url, str(dest))
    assert not dest.exists()


def test_cancel_raises_and_keeps_parts_for_resume(server, tmp_path, monkeypatch):
    dest = tmp_path / 'out.mp4'
    real = httpdl._safe_cb

    def stop_midway(cb, done, total):
        if done > len(PAYLOAD) // 3:
            crawler.request_stop()
        real(cb, done, total)

    monkeypatch.setattr(httpdl, '_safe_cb', stop_midway)
    with pytest.raises(IOError, match='取消'):
        httpdl.download_file(server.url, str(dest))
    assert not dest.exists()
    assert (tmp_path / 'out.mp4.parts.json').exists()


def test_small_file_stays_single_stream(server, tmp_path, monkeypatch):
    monkeypatch.setattr(httpdl, 'MIN_PART', len(PAYLOAD))
    dest = tmp_path / 'out.mp4'
    httpdl.download_file(server.url, str(dest))
    assert dest.read_bytes() == PAYLOAD and server.seen == ['bytes=0-']
