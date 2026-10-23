"""download.download() on the adapter HLS path, end to end, with only the browser faked.

The captured-stream work rewired things this path shares (movies_dir, the segment
crawler's progress reporting, the yt-dlp branch point). Nothing else exercises
download.py -- Selenium is the one thing a test cannot start -- so this runs the
real pipeline against a local server: page -> m3u8 -> disguised segments (the
fake-PNG-header trick SupJav uses) -> merge -> FFmpeg remux -> movies/.
"""

import http.server
import os
import shutil
import subprocess
import threading

import pytest

import crawler
import download

pytestmark = pytest.mark.skipif(not shutil.which('ffmpeg'), reason='needs FFmpeg')

FAKE_HEADER = b'\x89PNG\r\n\x1a\n' + b'\x00' * 40       # what a disguised CDN prepends


class _Driver:
    title = 'SJ-001 A title'

    def get(self, url):
        pass

    def quit(self):
        pass


@pytest.fixture
def hls_site(tmp_path):
    src = tmp_path / 'src'
    src.mkdir()
    subprocess.run(
        ['ffmpeg', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc=size=160x120:rate=25',
         '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '4', '-c:v', 'libx264', '-g', '25', '-c:a', 'aac',
         '-f', 'hls', '-hls_time', '1', '-hls_playlist_type', 'vod', str(src / 'index.m3u8')],
        check=True)
    seen = []

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(src), **k)

        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.append(dict(self.headers))
            if self.path.endswith('.ts'):
                data = FAKE_HEADER + (src / self.path.lstrip('/')).read_bytes()
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                super().do_GET()

    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{httpd.server_address[1]}', seen
    httpd.shutdown()
    httpd.server_close()


def test_adapter_hls_download_with_disguised_segments(hls_site, tmp_path, sites_dir, monkeypatch):
    from conftest import write_adapter
    base, seen = hls_site
    write_adapter(sites_dir, 'sj', {'id': 'sj', 'name': 'SJ', 'domains': ['127.0.0.1'],
                                    'headers': {'Referer': '{page_url}'},
                                    'download': {'strip_fake_header': True}})
    monkeypatch.setenv('OASIS_MEDIA_ROOT', str(tmp_path / 'media'))
    monkeypatch.setattr(download.webdriver, 'Chrome', lambda **kw: _Driver())
    monkeypatch.setattr(download, 'setup_driver_for_site', lambda options, adapter: options)
    monkeypatch.setattr(download, 'chrome_service', lambda **kw: None)
    monkeypatch.setattr(download, 'wait_for_page_load', lambda dr, adapter: None)
    monkeypatch.setattr(download, 'get_video_name', lambda dr, adapter: 'SJ-001 A title')
    monkeypatch.setattr(download, 'get_m3u8_url', lambda dr, adapter: f'{base}/index.m3u8')
    crawler.reset_stop()
    progress = []

    result = download.download(f'{base}/watch/1', progress_cb=progress.append)

    assert result is None                       # adapter sites are found by name, as before
    movies = tmp_path / 'media' / 'movies'
    assert [p.name for p in movies.iterdir()] == ['SJ-001 A title.mp4']      # work folder removed
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0',
                          str(movies / 'SJ-001 A title.mp4')], capture_output=True, text=True, check=True).stdout
    assert 3.5 < float(out) < 4.5
    assert progress[0] == 1 and progress[-1] == 100 and progress == sorted(progress)
    assert any(h.get('Referer') == f'{base}/watch/1' for h in seen)          # adapter headers still sent
