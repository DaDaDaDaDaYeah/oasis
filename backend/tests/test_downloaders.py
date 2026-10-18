"""httpdl.py and crawler.py against a real local HTTP server.

The properties that matter are the ones that would otherwise corrupt a library
silently: a resumed download must never be stitched onto a full-file response, a
truncated or cancelled download must raise rather than return short (the caller's
next step is promoting the file into movies/), and segment decryption must not
share cipher state.
"""

import http.server
import os
import re
import threading

import pytest
from Crypto.Cipher import AES

import crawler
import httpdl

PAYLOAD = bytes(range(256)) * 400          # ~100 KB, spans several chunks


class _Handler(http.server.BaseHTTPRequestHandler):
    """Configurable per-server behaviour via class attributes set by the fixture."""

    payload = PAYLOAD
    support_range = True
    truncate_to = None           # send a short body while advertising the full length
    omit_length = False          # close-delimited body with no Content-Length at all
    status_override = None
    seen = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).seen.append({'path': self.path, 'headers': dict(self.headers)})
        if self.status_override:
            self.send_response(self.status_override)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        data = self.payload
        rng = self.headers.get('Range')
        start = 0
        if rng and self.support_range:
            start = int(re.match(r'bytes=(\d+)-', rng).group(1))
            if start >= len(data):
                self.send_response(416)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            self.send_response(206)
        else:
            self.send_response(200)
        body = data[start:]
        if not self.omit_length:
            self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if self.truncate_to is not None:
            body = body[: self.truncate_to]
        self.wfile.write(body)
        self.wfile.flush()
        if self.truncate_to is not None:
            self.close_connection = True


@pytest.fixture
def server():
    handler = type('H', (_Handler,), {'seen': []})
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    handler.url = f'http://127.0.0.1:{httpd.server_address[1]}/video.mp4'
    yield handler
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture(autouse=True)
def _clean_stop_flag():
    crawler.reset_stop()
    yield
    crawler.reset_stop()


# --- httpdl ------------------------------------------------------------------

def test_fresh_download_writes_whole_file_and_reports_progress(server, tmp_path):
    dest = tmp_path / 'out.mp4'
    calls = []
    size = httpdl.download_file(server.url, str(dest), progress_cb=lambda d, t: calls.append((d, t)))
    assert size == len(PAYLOAD) and dest.read_bytes() == PAYLOAD
    assert calls[0] == (0, len(PAYLOAD)) and calls[-1] == (len(PAYLOAD), len(PAYLOAD))
    assert [d for d, _ in calls] == sorted(d for d, _ in calls)      # only ever forward


def test_resume_appends_from_partial_using_range(server, tmp_path):
    dest = tmp_path / 'out.mp4'
    dest.write_bytes(PAYLOAD[:30000])
    size = httpdl.download_file(server.url, str(dest))
    assert dest.read_bytes() == PAYLOAD and size == len(PAYLOAD)
    assert server.seen[0]['headers']['Range'] == 'bytes=30000-'


def test_server_ignoring_range_restarts_instead_of_corrupting(server, tmp_path):
    """A 200 to a ranged request carries the whole file; appending it to the
    partial would silently corrupt the video."""
    server.support_range = False
    dest = tmp_path / 'out.mp4'
    dest.write_bytes(b'JUNK-PARTIAL')
    httpdl.download_file(server.url, str(dest))
    assert dest.read_bytes() == PAYLOAD


def test_already_complete_file_is_a_noop_via_416(server, tmp_path):
    dest = tmp_path / 'out.mp4'
    dest.write_bytes(PAYLOAD)
    calls = []
    assert httpdl.download_file(server.url, str(dest), progress_cb=lambda d, t: calls.append((d, t))) == len(PAYLOAD)
    assert dest.read_bytes() == PAYLOAD and calls == [(len(PAYLOAD), len(PAYLOAD))]


def test_truncated_response_raises_so_a_short_file_is_never_promoted(server, tmp_path):
    server.truncate_to = 5000
    dest = tmp_path / 'out.mp4'
    with pytest.raises(Exception):
        httpdl.download_file(server.url, str(dest))
    # The failure is raised, so the caller never moves the short file into movies/.
    assert not dest.exists() or dest.stat().st_size < len(PAYLOAD)


def test_download_after_a_failed_attempt_completes(server, tmp_path):
    dest = tmp_path / 'out.mp4'
    server.truncate_to = 5000
    with pytest.raises(Exception):
        httpdl.download_file(server.url, str(dest))
    server.truncate_to = None
    httpdl.download_file(server.url, str(dest))
    assert dest.read_bytes() == PAYLOAD


def test_http_error_status_raises(server, tmp_path):
    server.status_override = 404
    with pytest.raises(Exception):
        httpdl.download_file(server.url, str(tmp_path / 'out.mp4'))


def test_cancel_raises_and_leaves_partial(server, tmp_path, monkeypatch):
    monkeypatch.setattr(httpdl, 'CHUNK_SIZE', 4096)       # the payload must span several chunks
    dest = tmp_path / 'out.mp4'

    def cancel_after_first_chunk(done, total):
        if done > 0:
            crawler.request_stop()

    with pytest.raises(IOError):
        httpdl.download_file(server.url, str(dest), progress_cb=cancel_after_first_chunk)
    assert dest.stat().st_size < len(PAYLOAD)


def test_cancel_raises_even_when_the_size_is_unknown(server, tmp_path, monkeypatch):
    """Without a Content-Length there is no expected total to fall short of, so
    the explicit cancel check is the only thing standing between a cancelled
    partial file and the caller promoting it into movies/."""
    monkeypatch.setattr(httpdl, 'CHUNK_SIZE', 4096)
    server.omit_length = True
    checks = {'n': 0}

    def stop_after_two_chunks():
        checks['n'] += 1
        return checks['n'] > 2

    monkeypatch.setattr(httpdl, 'is_stop_requested', stop_after_two_chunks)
    dest = tmp_path / 'out.mp4'
    with pytest.raises(IOError, match='取消'):
        httpdl.download_file(server.url, str(dest))
    assert 0 < dest.stat().st_size < len(PAYLOAD)      # partial kept for a resume


class _StubResponse:
    status_code = 200

    def __init__(self, headers, chunks):
        self.headers, self._chunks = headers, chunks

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=None):
        yield from self._chunks

    def close(self):
        pass


class _StubSession:
    def __init__(self, response):
        self._response = response

    def get(self, *a, **k):
        return self._response


def test_a_clean_but_short_body_raises_rather_than_returning(tmp_path, monkeypatch):
    """Defence in depth: if a body ends cleanly with fewer bytes than the server
    promised (no transport error to surface it), the file must still not be
    returned as complete — the caller's next step is moving it into movies/."""
    resp = _StubResponse({'Content-Length': '1000'}, [b'x' * 400])
    monkeypatch.setattr(httpdl, '_make_session', lambda headers: _StubSession(resp))
    dest = tmp_path / 'out.mp4'
    with pytest.raises(IOError, match='400 / 1000'):
        httpdl.download_file('http://unused.test/v.mp4', str(dest))


def test_an_exact_length_body_is_accepted(tmp_path, monkeypatch):
    resp = _StubResponse({'Content-Length': '600'}, [b'a' * 400, b'b' * 200])
    monkeypatch.setattr(httpdl, '_make_session', lambda headers: _StubSession(resp))
    dest = tmp_path / 'out.mp4'
    assert httpdl.download_file('http://unused.test/v.mp4', str(dest)) == 600
    assert dest.read_bytes() == b'a' * 400 + b'b' * 200


def test_stop_flag_from_previous_run_does_not_block_a_new_download(server, tmp_path):
    crawler.request_stop()
    dest = tmp_path / 'out.mp4'
    httpdl.download_file(server.url, str(dest))           # reset_stop() at start
    assert dest.read_bytes() == PAYLOAD


def test_failing_progress_callback_never_aborts_the_download(server, tmp_path):
    def boom(done, total):
        raise RuntimeError('sink is down')
    dest = tmp_path / 'out.mp4'
    httpdl.download_file(server.url, str(dest), progress_cb=boom)
    assert dest.read_bytes() == PAYLOAD


def test_headers_and_cookies_are_sent(server, tmp_path):
    httpdl.download_file(server.url, str(tmp_path / 'o.mp4'),
                         headers={'Referer': 'https://page.test/v'}, cookies={'sig': 'abc'})
    sent = server.seen[0]['headers']
    assert sent['Referer'] == 'https://page.test/v' and 'sig=abc' in sent['Cookie']


# --- crawler: pure helpers ---------------------------------------------------

def ts_packets(n=6):
    """A plausible MPEG-TS run: sync byte 0x47 on a 188-byte stride."""
    return b''.join(b'\x47' + bytes([i]) * 187 for i in range(n))


def test_strip_fake_header_passthrough_for_real_ts():
    data = ts_packets()
    assert crawler._strip_fake_header(data) == data


def test_strip_fake_header_removes_disguise_prefix():
    """CDNs prepend a fake PNG header so the segment looks like an image."""
    fake = b'\x89PNG\r\n\x1a\n' + b'\x00' * 37
    data = fake + ts_packets(8)
    assert crawler._strip_fake_header(data) == ts_packets(8)


def test_strip_fake_header_skips_a_stray_sync_byte_in_the_prefix():
    prefix = b'junk\x47junk\x47'                      # 0x47 that is NOT followed by a stride
    assert crawler._strip_fake_header(prefix + ts_packets(8)) == ts_packets(8)


@pytest.mark.parametrize('payload', [b'', b'<html>error page</html>', b'\x00' * 5000, b'\x89PNG' + b'\x00' * 100])
def test_strip_fake_header_rejects_non_ts_payloads(payload):
    """An error page must come back empty so it is never merged into the video."""
    assert crawler._strip_fake_header(payload) == b''


def test_segment_filename_is_filesystem_safe_and_bounded():
    name = crawler.segment_filename('https://cdn.test/path/seg0001.ts?token=a/b:c*d&x=1.ts')
    assert name.endswith('.mp4') and len(name) <= 104
    assert not set('/\\:*?"<>|') & set(name)


def test_segment_filename_is_deterministic_and_distinct():
    a = crawler.segment_filename('https://cdn.test/seg1.ts')
    b = crawler.segment_filename('https://cdn.test/seg2.ts')
    assert a == crawler.segment_filename('https://cdn.test/seg1.ts') and a != b


# --- crawler: scraping segments ----------------------------------------------

class SegmentHandler(http.server.BaseHTTPRequestHandler):
    bodies = {}
    fail_first = set()
    hits = {}

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).hits[self.path] = type(self).hits.get(self.path, 0) + 1
        if self.path in type(self).fail_first and type(self).hits[self.path] == 1:
            self.send_response(503)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        body = type(self).bodies.get(self.path)
        if body is None:
            self.send_response(404)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def segments():
    handler = type('S', (SegmentHandler,), {'bodies': {}, 'fail_first': set(), 'hits': {}})
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    handler.base = f'http://127.0.0.1:{httpd.server_address[1]}'
    yield handler
    httpd.shutdown()
    httpd.server_close()


def test_crawl_downloads_every_segment_and_reports_progress(segments, tmp_path, monkeypatch):
    urls = []
    for i in range(10):
        segments.bodies[f'/seg{i}.ts'] = ts_packets(6)
        urls.append(f'{segments.base}/seg{i}.ts')
    progress = []
    crawler.prepareCrawl(None, None, str(tmp_path), urls, dl_headers={},
                         progress_cb=lambda done, total: progress.append((done, total)))
    for u in urls:
        assert (tmp_path / crawler.segment_filename(u)).read_bytes() == ts_packets(6)
    assert progress and progress[-1] == (10, 10) and all(t == 10 for _, t in progress)


def test_crawl_retries_failed_segments_in_later_rounds(segments, tmp_path, monkeypatch):
    monkeypatch.setattr(crawler._stop_event, 'wait', lambda timeout=None: False)   # no real backoff sleep
    segments.bodies['/a.ts'] = ts_packets()
    segments.fail_first.add('/a.ts')
    url = f'{segments.base}/a.ts'
    crawler.prepareCrawl(None, None, str(tmp_path), [url], dl_headers={})
    assert (tmp_path / crawler.segment_filename(url)).exists()
    assert segments.hits['/a.ts'] == 2


def test_existing_segments_are_not_fetched_again(segments, tmp_path):
    """The resume contract: a restart keeps what is on disk."""
    url = f'{segments.base}/done.ts'
    (tmp_path / crawler.segment_filename(url)).write_bytes(b'already here')
    crawler.prepareCrawl(None, None, str(tmp_path), [url], dl_headers={})
    assert segments.hits == {}
    assert (tmp_path / crawler.segment_filename(url)).read_bytes() == b'already here'


def test_encrypted_segments_decrypt_with_independent_ciphers(segments, tmp_path):
    key, iv = b'k' * 16, b'i' * 16
    plains = {}
    urls = []
    for i in range(12):
        plain = ts_packets(4 + i % 3)
        plains[i] = plain
        padded = plain + b'\x00' * ((-len(plain)) % 16)          # CBC needs whole blocks
        segments.bodies[f'/e{i}.ts'] = AES.new(key, AES.MODE_CBC, iv).encrypt(padded)
        urls.append(f'{segments.base}/e{i}.ts')
    crawler.prepareCrawl(key, iv, str(tmp_path), urls, dl_headers={})
    for i, u in enumerate(urls):
        got = (tmp_path / crawler.segment_filename(u)).read_bytes()
        assert got.startswith(plains[i])           # CBC state is per segment, never shared across threads


def test_disguised_segments_are_unwrapped_and_error_pages_are_not_written(segments, tmp_path):
    good = f'{segments.base}/good.ts'
    bad = f'{segments.base}/bad.ts'
    segments.bodies['/good.ts'] = b'\x89PNG\r\n\x1a\n' + b'\x00' * 20 + ts_packets(8)
    segments.bodies['/bad.ts'] = b'<html>blocked</html>'
    # Call scrape() directly (one round) so the never-valid segment can't loop.
    sess = crawler._make_session({})
    lock, remaining = threading.Lock(), {good, bad}
    crawler.reset_stop()
    ok_good = crawler.scrape(None, None, str(tmp_path), remaining, lock, sess, 2, None, True, good)
    ok_bad = crawler.scrape(None, None, str(tmp_path), remaining, lock, sess, 2, None, True, bad)
    assert ok_good is True and ok_bad is False
    assert (tmp_path / crawler.segment_filename(good)).read_bytes() == ts_packets(8)
    assert not (tmp_path / crawler.segment_filename(bad)).exists()      # corrupt bytes never land on disk
    assert remaining == {bad}


def test_scrape_short_circuits_after_stop_requested(segments, tmp_path):
    segments.bodies['/x.ts'] = ts_packets()
    url = f'{segments.base}/x.ts'
    crawler.request_stop()
    sess = crawler._make_session({})
    assert crawler.scrape(None, None, str(tmp_path), {url}, threading.Lock(), sess, 1, None, False, url) is False
    assert segments.hits == {} and os.listdir(tmp_path) == []


def test_stop_during_crawl_exits_the_retry_loop(segments, tmp_path):
    """A permanently failing segment would otherwise retry forever; a cancel has
    to end the loop promptly."""
    url = f'{segments.base}/missing.ts'                     # 404 forever
    threading.Timer(0.3, crawler.request_stop).start()
    crawler.prepareCrawl(None, None, str(tmp_path), [url], dl_headers={})
    assert os.listdir(tmp_path) == []
