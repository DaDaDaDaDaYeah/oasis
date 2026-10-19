"""Captured streams: the /api/capture intake (capture.py) and the adapter-free
download (capture_dl.py).

The intake guards worth pinning: the header allow-list (a forwarded Host or a
CR/LF must never reach the CDN request), the private-network refusal for remote
callers (the backend would otherwise fetch the owner's LAN for them and stream
the result back), and idempotency on the stream URL (a double-sent capture must
not file the video twice).
"""

import http.server
import shutil
import socket
import subprocess
import threading

import pytest

import api
import auth
import capture
import capture_dl
import catalog
import crawler
from test_api import CLIENT_HDR, _isolated, local, make_client, media  # noqa: F401  (fixtures)


@pytest.fixture
def queued(monkeypatch):
    calls = []
    monkeypatch.setattr(api, '_start_download', lambda vid, url: calls.append((vid, url)))
    return calls


@pytest.fixture
def public_dns(monkeypatch):
    """Resolve every hostname to a public address so tests never hit DNS."""
    real = socket.getaddrinfo

    def fake(host, *a, **k):
        if host in ('cdn.example', 'site.example'):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 0))]
        return real(host, *a, **k)

    monkeypatch.setattr(socket, 'getaddrinfo', fake)


# --- capture.py --------------------------------------------------------------

def test_detect_kind():
    assert capture.detect_kind('https://c/x/master.m3u8?sig=1') == 'hls'
    assert capture.detect_kind('https://c/x/manifest.mpd') == 'dash'
    assert capture.detect_kind('https://c/x/video.mp4') == 'http'
    assert capture.detect_kind('https://c/play?id=3', content_type='application/vnd.apple.mpegurl') == 'hls'
    assert capture.detect_kind('https://c/play?id=3', content_type='application/dash+xml') == 'dash'
    assert capture.detect_kind('https://c/a.mp4', hint='HLS') == 'hls'
    assert capture.detect_kind('https://c/a.mp4', hint='bogus') == 'http'


def test_headers_are_allow_listed_and_injection_proof():
    out = capture.clean_headers({
        'referer': 'https://site.example/watch',
        'Cookie': 'a=1',
        'Host': 'evil.example',
        'Range': 'bytes=0-',
        'User-Agent': 'UA\r\nX-Injected: 1',
        'Origin': 'x' * 9000,
        'Accept-Language': 123,
    })
    assert out == {'Referer': 'https://site.example/watch', 'Cookie': 'a=1'}
    assert capture.clean_headers(['not', 'a', 'dict']) == {}


@pytest.mark.parametrize('url', [
    'http://127.0.0.1/x.mp4', 'http://localhost/x.mp4', 'http://192.168.1.1/admin',
    'http://10.0.0.5/x.m3u8', 'http://169.254.169.254/latest/meta-data', 'http://[::1]/x',
    'http://[::ffff:192.168.0.1]/x',
])
def test_remote_callers_cannot_target_the_private_network(url):
    with pytest.raises(capture.CaptureError):
        capture.check_destination(url, allow_private=False)
    capture.check_destination(url, allow_private=True)   # the owner at the machine may


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'ftp://h/x', 'javascript:alert(1)', 'http:///nohost'])
def test_non_http_urls_are_refused_for_everyone(url):
    with pytest.raises(capture.CaptureError):
        capture.check_destination(url, allow_private=True)


# --- /api/capture ------------------------------------------------------------

def _body(**kw):
    return {'page_url': 'https://site.example/watch/1', 'media_url': 'https://cdn.example/v/master.m3u8',
            'title': 'A nice clip', 'headers': {'Referer': 'https://site.example/watch/1', 'Host': 'x'}, **kw}


def test_capture_files_a_video_stores_the_stream_and_queues_it(local, queued, public_dns):
    r = local.post('/api/capture', json=_body())
    assert r.status_code == 200, r.text
    data = r.json()
    assert data['status'] == 'created' and data['download_started']
    vid = data['record']['id']
    assert data['record']['url'] == 'https://site.example/watch/1'
    assert data['record']['title'] == 'A nice clip'
    cap = capture.get(vid)
    assert cap['kind'] == 'hls' and cap['media_url'] == 'https://cdn.example/v/master.m3u8'
    assert cap['headers'] == {'Referer': 'https://site.example/watch/1'}
    assert queued == [(vid, 'https://site.example/watch/1')]


def test_same_stream_twice_reuses_the_entry(local, queued, public_dns):
    first = local.post('/api/capture', json=_body()).json()
    second = local.post('/api/capture', json=_body(headers={'Cookie': 'fresh=1'})).json()
    assert second['status'] == 'exists' and second['record']['id'] == first['record']['id']
    assert len(catalog.list_all_videos()) == 1
    # The re-send refreshes the headers, since signed cookies expire.
    assert capture.get(first['record']['id'])['headers'] == {'Cookie': 'fresh=1'}


def test_title_with_known_code_attaches_to_the_existing_video(local, queued, public_dns):
    from conftest import video
    vid = catalog.insert_video(video('ABC-123'))
    r = local.post('/api/capture', json=_body(title='ABC-123 something')).json()
    assert r['status'] == 'exists' and r['record']['id'] == vid
    assert capture.get(vid) is not None


def test_no_download_flag_only_files_it(local, queued, public_dns):
    r = local.post('/api/capture', json=_body(download=False)).json()
    assert r['download_started'] is False and queued == []


def test_remote_caller_with_code_is_refused_a_lan_target(queued, public_dns):
    code = auth.enable()
    c = make_client('lan')
    r = c.post('/api/capture', json=_body(media_url='http://192.168.1.1/video.mp4'),
               headers={**CLIENT_HDR, 'Authorization': f'Bearer {code}'})
    assert r.status_code == 400 and queued == []
    r = c.post('/api/capture', json=_body(), headers={**CLIENT_HDR, 'Authorization': f'Bearer {code}'})
    assert r.status_code == 200


def test_capture_requires_auth_like_every_endpoint(queued, public_dns):
    auth.enable()
    r = make_client('lan').post('/api/capture', json=_body())
    assert r.status_code == 401 and queued == []


def test_deleting_the_video_drops_its_capture(local, queued, public_dns):
    vid = local.post('/api/capture', json=_body()).json()['record']['id']
    assert local.delete(f'/api/videos/{vid}').status_code == 200
    assert capture.get(vid) is None


# --- capture_dl, end to end ----------------------------------------------------

needs_ffmpeg = pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),
                                  reason='needs FFmpeg')


def _serve(directory, seen=None):
    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(directory), **k)

        def log_message(self, *a):
            pass

        def do_GET(self):
            if seen is not None:
                seen.append(dict(self.headers))
            super().do_GET()

    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f'http://127.0.0.1:{httpd.server_address[1]}'


def _probe(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type:format=duration',
                          '-of', 'csv=p=0', str(path)], capture_output=True, text=True, check=True).stdout
    kinds = {l for l in out.split() if l in ('video', 'audio')}
    duration = float([l for l in out.split() if l.replace('.', '').isdigit()][0])
    return kinds, duration


def _ffmpeg(*args):
    subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc=size=160x120:rate=25',
                    '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '4', '-c:v', 'libx264', '-g', '25',
                    '-c:a', 'aac', *args], check=True)


@pytest.fixture
def movies(tmp_path, monkeypatch):
    monkeypatch.setenv('OASIS_MEDIA_ROOT', str(tmp_path / 'media'))
    crawler.reset_stop()
    yield tmp_path / 'media' / 'movies'
    crawler.reset_stop()


@needs_ffmpeg
def test_encrypted_ts_hls_downloads_with_sequence_number_ivs(tmp_path, movies):
    src = tmp_path / 'src'
    src.mkdir()
    (src / 'k.key').write_bytes(bytes(range(16)))
    # No IV in the key info file: FFmpeg then uses the media sequence number,
    # the spec default the downloader must reproduce.
    (src / 'keyinfo').write_text(f'k.key\n{src / "k.key"}\n')
    _ffmpeg('-f', 'hls', '-hls_time', '1', '-hls_playlist_type', 'vod', '-start_number', '7',
            '-hls_key_info_file', str(src / 'keyinfo'), str(src / 'index.m3u8'))
    seen = []
    httpd, base = _serve(src, seen)
    try:
        cap = {'kind': 'hls', 'media_url': f'{base}/index.m3u8', 'headers': {'Referer': 'https://site.example/'}}
        rel = capture_dl.download({'id': 1, 'code': 'MANUAL-1', 'title': 'clip'}, cap)
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert rel == 'movies/clip.mp4'
    kinds, duration = _probe(movies / 'clip.mp4')
    assert kinds == {'video', 'audio'} and 3.5 < duration < 4.5
    assert all(h.get('Referer') == 'https://site.example/' for h in seen)
    assert sorted(p.name for p in movies.iterdir()) == ['clip.mp4']   # work folder removed


@needs_ffmpeg
def test_fmp4_master_playlist_with_separate_audio_rendition(tmp_path, movies):
    src = tmp_path / 'src'
    src.mkdir()
    _ffmpeg('-map', '0:v', '-map', '1:a', '-f', 'hls', '-hls_time', '1', '-hls_playlist_type', 'vod',
            '-hls_segment_type', 'fmp4', '-master_pl_name', 'master.m3u8',
            '-var_stream_map', 'v:0,agroup:aud a:0,agroup:aud', '-hls_segment_filename',
            str(src / 's%v_%03d.m4s'), str(src / 'p%v.m3u8'))
    httpd, base = _serve(src)
    try:
        cap = {'kind': 'hls', 'media_url': f'{base}/master.m3u8', 'headers': {}}
        capture_dl.download({'id': 2, 'code': 'MANUAL-2', 'title': 'split'}, cap)
    finally:
        httpd.shutdown()
        httpd.server_close()
    kinds, duration = _probe(movies / 'split.mp4')
    assert kinds == {'video', 'audio'} and 3.5 < duration < 4.5


def test_expired_link_returning_html_is_not_promoted(tmp_path, movies):
    src = tmp_path / 'src'
    src.mkdir()
    (src / 'video.mp4').write_text('<!DOCTYPE html><html>Link expired</html>')
    httpd, base = _serve(src)
    try:
        with pytest.raises(capture_dl.CaptureDownloadError):
            capture_dl.download({'id': 3, 'code': 'MANUAL-3', 'title': 'gone'},
                                {'kind': 'http', 'media_url': f'{base}/video.mp4', 'headers': {}})
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert not (movies / 'gone.mp4').exists()


def test_live_hls_is_refused(tmp_path, movies):
    src = tmp_path / 'src'
    src.mkdir()
    (src / 'live.m3u8').write_text('#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\na.ts\n')
    httpd, base = _serve(src)
    try:
        with pytest.raises(capture_dl.CaptureDownloadError, match='直播'):
            capture_dl.download({'id': 4, 'code': 'MANUAL-4', 'title': 'live'},
                                {'kind': 'hls', 'media_url': f'{base}/live.m3u8', 'headers': {}})
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_name_collision_gets_the_code_appended(movies):
    movies.mkdir(parents=True, exist_ok=True)
    (movies / 'clip.mp4').write_bytes(b'other video')
    assert capture_dl.target_name({'id': 9, 'code': 'MANUAL-9', 'title': 'clip'}) == 'clip [MANUAL-9]'
    assert capture_dl.target_name({'id': 9, 'code': 'MANUAL-9', 'title': 'a/b:c?'}) == 'abc'


def test_aes_ivs_follow_the_spec(monkeypatch):
    """Without an explicit IV the media sequence number is the IV. Getting it
    wrong only garbles each segment's first 16 bytes, which still probes as a
    valid video, so this is pinned at the planning step rather than end to end."""
    import m3u8
    text = ('#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:7\n'
            '#EXT-X-KEY:METHOD=AES-128,URI="k.key"\n#EXTINF:2,\na.ts\n#EXTINF:2,\nb.ts\n'
            '#EXT-X-KEY:METHOD=AES-128,URI="k2.key",IV=0x000102030405060708090A0B0C0D0E0F\n#EXTINF:2,\nc.ts\n'
            '#EXT-X-ENDLIST\n')
    monkeypatch.setattr(capture_dl, '_get', lambda url, h: type('R', (), {'content': url.encode().ljust(16)})())
    jobs, fmp4 = capture_dl._plan_track(m3u8.loads(text, uri='https://c/p/i.m3u8'), 'https://c/p/i.m3u8', {}, 'v', {})
    assert not fmp4
    assert [j['iv'] for j in jobs] == [(7).to_bytes(16, 'big'), (8).to_bytes(16, 'big'), bytes(range(16))]
    assert jobs[2]['key'] != jobs[0]['key']          # rotated key fetched separately


def test_sample_aes_is_refused_as_drm():
    import m3u8
    text = ('#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://x"\n'
            '#EXTINF:2,\na.ts\n#EXT-X-ENDLIST\n')
    with pytest.raises(capture_dl.CaptureDownloadError, match='DRM'):
        capture_dl._plan_track(m3u8.loads(text, uri='https://c/i.m3u8'), 'https://c/i.m3u8', {}, 'v', {})


# --- /api/analyze with a pasted stream URL --------------------------------------

def test_pasted_stream_url_is_filed_as_a_capture(local, queued, public_dns, sites_dir):
    r = local.post('/api/analyze', json={'url': 'https://cdn.example/show/ep01.m3u8', 'download': True,
                                         'task_id': 't1'})
    assert r.status_code == 200 and r.json()['status'] == 'success'
    vid = r.json()['id']
    assert capture.get(vid)['kind'] == 'hls'
    assert catalog.get_video_by_id(vid)['title'] == 'ep01'
    assert queued == [(vid, 'https://cdn.example/show/ep01.m3u8')]
    # The frontend's poller sees an ordinary finished analysis.
    s = local.get('/api/analyze/t1/status').json()
    assert s['status'] == 'success' and s['ids'] == [vid]


def test_generic_manifest_name_is_titled_after_the_host(local, queued, public_dns, sites_dir):
    vid = local.post('/api/analyze', json={'url': 'https://cdn.example/x/master.m3u8'}).json()['id']
    assert catalog.get_video_by_id(vid)['title'] == 'cdn.example'
    assert queued == []      # download not asked for


def test_adapter_sites_keep_their_own_pipeline(local, queued, public_dns, sites_dir, monkeypatch):
    from conftest import write_adapter
    write_adapter(sites_dir, 'ex', {'id': 'ex', 'name': 'Ex', 'domains': ['cdn.example']})
    spawned = []
    monkeypatch.setattr(api.multiprocessing, 'Process',
                        lambda **kw: type('P', (), {'start': lambda self: spawned.append(kw)})())
    r = local.post('/api/analyze', json={'url': 'https://cdn.example/v.mp4'})
    assert r.json()['status'] == 'analyzing' and spawned and capture._by_media_url('https://cdn.example/v.mp4') is None


def test_ordinary_page_urls_are_untouched(local, queued, public_dns, sites_dir, monkeypatch):
    spawned = []
    monkeypatch.setattr(api.multiprocessing, 'Process',
                        lambda **kw: type('P', (), {'start': lambda self: spawned.append(kw)})())
    assert local.post('/api/analyze', json={'url': 'https://site.example/watch/1'}).json()['status'] == 'analyzing'
    assert spawned


@needs_ffmpeg
def test_worker_end_to_end_capture_to_streamable_file(tmp_path, local, media, monkeypatch, queued):
    """The whole wiring, minus the process boundary: /api/capture files it, the
    download worker (run in-process) picks the capture path, the DB records the
    file, and /api/stream serves it."""
    monkeypatch.setenv('OASIS_MEDIA_ROOT', str(media))
    src = tmp_path / 'src'
    src.mkdir()
    _ffmpeg('-f', 'hls', '-hls_time', '1', '-hls_playlist_type', 'vod', str(src / 'index.m3u8'))
    httpd, base = _serve(src)
    try:
        r = local.post('/api/capture', json={'page_url': 'https://site.example/watch/9',
                                            'media_url': f'{base}/index.m3u8', 'title': 'Wired up'})
        vid = r.json()['record']['id']
        crawler.reset_stop()
        api._download_worker(vid, 'https://site.example/watch/9')
    finally:
        httpd.shutdown()
        httpd.server_close()
        crawler.reset_stop()
    record = catalog.get_video_by_id(vid)
    assert record['video_path'] == 'movies/Wired up.mp4'
    s = local.get(f'/api/stream/{vid}')
    assert s.status_code == 200 and len(s.content) > 10000
