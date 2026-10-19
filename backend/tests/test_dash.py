"""dash.py: manifest planning, and one end-to-end run when FFmpeg is present.

Planning is where a DASH downloader goes wrong quietly — a mis-expanded
SegmentTimeline or a dropped BaseURL yields a video that downloads fine and then
stops halfway — so each addressing scheme is pinned here against a literal
manifest. DRM and live manifests must be refused, never half-downloaded.
"""

import http.server
import os
import shutil
import subprocess
import threading

import pytest

import crawler
import dash

NS = 'xmlns="urn:mpeg:dash:schema:mpd:2011"'


def _mpd(body, attrs='type="static" mediaPresentationDuration="PT10S"'):
    return f'<?xml version="1.0"?><MPD {NS} {attrs}>{body}</MPD>'


def test_parse_duration():
    assert dash.parse_duration('PT1H2M3.5S') == 3723.5
    assert dash.parse_duration('PT6.0S') == 6.0
    assert dash.parse_duration('P1DT1S') == 86401
    assert dash.parse_duration(None) == 0.0 and dash.parse_duration('garbage') == 0.0


def test_template_with_timeline_picks_best_video_and_audio():
    body = '''<Period><AdaptationSet contentType="video">
      <SegmentTemplate timescale="10" initialization="init-$RepresentationID$.m4s"
                       media="seg-$RepresentationID$-$Number%03d$-$Time$.m4s" startNumber="5">
        <SegmentTimeline><S t="100" d="20" r="2"/><S d="10"/></SegmentTimeline></SegmentTemplate>
      <Representation id="lo" bandwidth="100" height="360"/>
      <Representation id="hi" bandwidth="900" height="1080"/>
    </AdaptationSet>
    <AdaptationSet mimeType="audio/mp4">
      <SegmentTemplate media="a-$Number$.m4s" initialization="a-init.m4s" duration="5" timescale="1"/>
      <Representation id="a1" bandwidth="64"/><Representation id="a2" bandwidth="128"/>
    </AdaptationSet></Period>'''
    p = dash.plan(_mpd(body), 'https://cdn.example/v/manifest.mpd')
    urls = [j['url'] for j in p['video']]
    assert urls == [
        'https://cdn.example/v/init-hi.m4s',
        'https://cdn.example/v/seg-hi-005-100.m4s',
        'https://cdn.example/v/seg-hi-006-120.m4s',
        'https://cdn.example/v/seg-hi-007-140.m4s',
        'https://cdn.example/v/seg-hi-008-160.m4s',
    ]
    # 10 s / 5 s per segment = 2 audio segments, numbered from 1, after the init.
    assert [j['url'].rsplit('/', 1)[1] for j in p['audio']] == ['a-init.m4s', 'a-1.m4s', 'a-2.m4s']
    # Names sort in playback order and never collide between tracks.
    names = [j['name'] for j in p['video'] + p['audio']]
    assert len(set(names)) == len(names) and [j['name'] for j in p['video']] == sorted(j['name'] for j in p['video'])


def test_timeline_repeat_minus_one_runs_to_period_end():
    body = '''<Period duration="PT4S"><AdaptationSet contentType="video">
      <Representation id="v" bandwidth="1"><SegmentTemplate timescale="1" media="$Time$.m4s">
      <SegmentTimeline><S t="0" d="1" r="-1"/></SegmentTimeline></SegmentTemplate></Representation>
    </AdaptationSet></Period>'''
    p = dash.plan(_mpd(body), 'http://h/m.mpd')
    assert [j['url'] for j in p['video']] == [f'http://h/{t}.m4s' for t in range(4)]
    assert p['audio'] is None


def test_base_url_chain_is_resolved_level_by_level():
    body = '''<BaseURL>https://other.example/root/</BaseURL><Period><BaseURL>p/</BaseURL>
      <AdaptationSet contentType="video"><BaseURL>v/</BaseURL>
      <Representation id="r" bandwidth="1"><BaseURL>file.mp4</BaseURL><SegmentBase indexRange="0-9"/>
      </Representation></AdaptationSet></Period>'''
    p = dash.plan(_mpd(body), 'https://cdn.example/m.mpd')
    assert [j['url'] for j in p['video']] == ['https://other.example/root/p/v/file.mp4']


def test_segment_list_with_byte_ranges():
    body = '''<Period><AdaptationSet contentType="video"><Representation id="r" bandwidth="1">
      <BaseURL>whole.mp4</BaseURL><SegmentList>
        <Initialization range="0-99"/><SegmentURL mediaRange="100-199"/><SegmentURL mediaRange="200-299"/>
      </SegmentList></Representation></AdaptationSet></Period>'''
    p = dash.plan(_mpd(body), 'https://cdn.example/x/m.mpd')
    assert [(j['url'], j['range']) for j in p['video']] == [
        ('https://cdn.example/x/whole.mp4', (0, 99)),
        ('https://cdn.example/x/whole.mp4', (100, 199)),
        ('https://cdn.example/x/whole.mp4', (200, 299)),
    ]


def test_drm_and_live_are_refused():
    drm = '''<Period><AdaptationSet contentType="video"><ContentProtection schemeIdUri="urn:x"/>
      <Representation id="r" bandwidth="1"><BaseURL>a.mp4</BaseURL></Representation></AdaptationSet></Period>'''
    with pytest.raises(dash.DashError, match='DRM'):
        dash.plan(_mpd(drm), 'http://h/m.mpd')
    live = '<Period><AdaptationSet contentType="video"><Representation id="r"/></AdaptationSet></Period>'
    with pytest.raises(dash.DashError, match='直播'):
        dash.plan(_mpd(live, 'type="dynamic"'), 'http://h/m.mpd')
    with pytest.raises(dash.DashError):
        dash.plan('<html>not a manifest</html>', 'http://h/m.mpd')


def test_runaway_segment_count_is_refused():
    body = '''<Period><AdaptationSet contentType="video"><Representation id="r" bandwidth="1">
      <SegmentTemplate media="$Number$" duration="1" timescale="1000"/></Representation>
    </AdaptationSet></Period>'''
    with pytest.raises(dash.DashError, match='過多'):
        dash.plan(_mpd(body, 'type="static" mediaPresentationDuration="PT10H"'), 'http://h/m.mpd')


# --- end to end ---------------------------------------------------------------

@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='needs FFmpeg')
def test_real_dash_stream_downloads_and_muxes(tmp_path):
    src = tmp_path / 'src'
    src.mkdir()
    subprocess.run(
        ['ffmpeg', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc=size=160x120:rate=25',
         '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '4', '-c:v', 'libx264', '-g', '25',
         '-c:a', 'aac', '-map', '0:v', '-map', '1:a', '-f', 'dash', '-seg_duration', '1',
         str(src / 'manifest.mpd')],
        check=True,
    )
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(src), **k)
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    httpd.RequestHandlerClass.log_message = lambda *a: None
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    crawler.reset_stop()
    try:
        work = tmp_path / 'work'
        out = tmp_path / 'out.mp4'
        calls = []
        dash.download(f'http://127.0.0.1:{httpd.server_address[1]}/manifest.mpd', str(work), str(out),
                      progress_cb=lambda d, t: calls.append((d, t)))
    finally:
        httpd.shutdown()
        httpd.server_close()
    probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type:format=duration',
                            '-of', 'csv=p=0', str(out)], capture_output=True, text=True, check=True).stdout
    assert 'video' in probe and 'audio' in probe
    duration = float([l for l in probe.split() if l.replace('.', '').isdigit()][0])
    assert 3.5 < duration < 4.5
    assert calls[-1][0] == calls[-1][1]
    # Only the muxed file survives; segments and per-track files are cleaned up.
    assert [p.name for p in work.iterdir()] == []
