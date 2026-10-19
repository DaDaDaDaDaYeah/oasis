"""MPEG-DASH (.mpd) downloader.

The third media pipeline, after HLS (crawler.py) and progressive MP4
(httpdl.py), modelled on XDM's DASH support. A DASH manifest lists video and
audio as *separate* tracks, each a sequence of fMP4 segments described by one
of three schemes; we pick the best video and the best audio, fetch each track's
init + media segments in order, concatenate them per track (fMP4 segments are
designed to be joined that way) and let FFmpeg mux the two into one MP4.

Scope, deliberately:
- **static** manifests only. A `type="dynamic"` MPD is a live stream with no end.
- **no DRM**. A track carrying `<ContentProtection>` is encrypted with keys we do
  not have and must not try to obtain; it is refused with a clear message rather
  than downloaded into an unplayable file.
"""

import math
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

import requests

import segments

_DUR_RE = re.compile(
    r'^P(?:(?P<y>[\d.]+)Y)?(?:(?P<mo>[\d.]+)M)?(?:(?P<w>[\d.]+)W)?(?:(?P<d>[\d.]+)D)?'
    r'(?:T(?:(?P<h>[\d.]+)H)?(?:(?P<mi>[\d.]+)M)?(?:(?P<s>[\d.]+)S)?)?$'
)
_TEMPLATE_RE = re.compile(r'\$(RepresentationID|Number|Time|Bandwidth)(%0(\d+)d)?\$')

# Hard stop on segment count: a malformed manifest (or a huge duration paired with
# a tiny segment length) must not make us enumerate millions of URLs.
MAX_SEGMENTS = 50000


class DashError(Exception):
    pass


def parse_duration(value: str | None) -> float:
    """ISO-8601 duration (PT1H2M3.5S) → seconds; 0.0 when absent or unparseable."""
    if not value:
        return 0.0
    m = _DUR_RE.match(value.strip())
    if not m:
        return 0.0
    g = {k: float(v) if v else 0.0 for k, v in m.groupdict().items()}
    return (((g['y'] * 365 + g['mo'] * 30 + g['w'] * 7 + g['d']) * 24 + g['h']) * 60 + g['mi']) * 60 + g['s']


def _local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


def _child(el, name):
    if el is None:
        return None
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _children(el, name):
    return [c for c in el if _local(c.tag) == name] if el is not None else []


def _base(url: str, *elements) -> str:
    """Resolve the BaseURL chain MPD → Period → AdaptationSet → Representation."""
    for el in elements:
        b = _child(el, 'BaseURL')
        if b is not None and (b.text or '').strip():
            url = urljoin(url, b.text.strip())
    return url


def _fill(template: str, rep_id: str, bandwidth, number=None, time=None) -> str:
    def sub(m):
        name, width = m.group(1), m.group(3)
        value = {'RepresentationID': rep_id, 'Number': number, 'Time': time,
                 'Bandwidth': bandwidth}[name]
        if value is None:
            raise DashError(f'範本缺少 ${name}$ 的值')
        if width and name != 'RepresentationID':
            return str(value).zfill(int(width))
        return str(value)
    return _TEMPLATE_RE.sub(sub, template).replace('$$', '$')


def _merged_template(*levels):
    """SegmentTemplate attributes inherit downward (AdaptationSet → Representation);
    the most specific level wins per attribute, and a SegmentTimeline is taken
    from the most specific level that has one."""
    attrs, timeline, found = {}, None, False
    for el in levels:
        t = _child(el, 'SegmentTemplate')
        if t is None:
            continue
        found = True
        attrs.update(t.attrib)
        tl = _child(t, 'SegmentTimeline')
        if tl is not None:
            timeline = tl
    return (attrs, timeline) if found else (None, None)


def _parse_range(value: str | None):
    if not value or '-' not in value:
        return None
    a, b = value.split('-', 1)
    return int(a), int(b)


def _template_segments(attrs, timeline, rep_id, bandwidth, base, period_duration):
    """(init_url, [media_url, ...]) for a SegmentTemplate."""
    timescale = int(attrs.get('timescale', 1))
    start_number = int(attrs.get('startNumber', 1))
    media = attrs.get('media')
    if not media:
        raise DashError('SegmentTemplate 缺少 media 屬性')
    init = attrs.get('initialization')
    init_url = urljoin(base, _fill(init, rep_id, bandwidth)) if init else None

    urls = []
    if timeline is not None:
        number = start_number
        t = 0
        entries = _children(timeline, 'S')
        end_time = period_duration * timescale if period_duration else None
        for idx, s in enumerate(entries):
            if 't' in s.attrib:
                t = int(s.attrib['t'])
            d = int(s.attrib['d'])
            r = int(s.attrib.get('r', 0))
            if r < 0:
                # r=-1 repeats until the next S's t, or the period's end.
                nxt = entries[idx + 1].attrib.get('t') if idx + 1 < len(entries) else None
                stop = int(nxt) if nxt is not None else end_time
                if not stop:
                    raise DashError('無法判斷 SegmentTimeline 的長度')
                r = max(0, math.ceil((stop - t) / d) - 1)
            for _ in range(r + 1):
                urls.append(urljoin(base, _fill(media, rep_id, bandwidth, number=number, time=t)))
                t += d
                number += 1
                if len(urls) > MAX_SEGMENTS:
                    raise DashError('片段數量過多，資訊清單可能有誤')
        return init_url, urls

    seg_duration = float(attrs.get('duration', 0))
    if not seg_duration or not period_duration:
        raise DashError('無法判斷片段數量（缺少 duration）')
    count = math.ceil(period_duration * timescale / seg_duration)
    if count > MAX_SEGMENTS:
        raise DashError('片段數量過多，資訊清單可能有誤')
    for i in range(count):
        n = start_number + i
        urls.append(urljoin(base, _fill(media, rep_id, bandwidth, number=n,
                                        time=int(i * seg_duration))))
    return init_url, urls


def _track_jobs(rep, adaptation, period, mpd, manifest_url, period_duration, prefix):
    """Download jobs for one Representation, in playback order (init first)."""
    rep_id = rep.attrib.get('id', '')
    bandwidth = rep.attrib.get('bandwidth')
    base = _base(manifest_url, mpd, period, adaptation, rep)
    jobs = []

    def add(url, rng=None):
        jobs.append({'url': url, 'name': f'{prefix}{len(jobs):06d}.seg', 'range': rng})

    attrs, timeline = _merged_template(period, adaptation, rep)
    if attrs is not None:
        init_url, urls = _template_segments(attrs, timeline, rep_id, bandwidth, base, period_duration)
        if init_url:
            add(init_url)
        for u in urls:
            add(u)
        return jobs

    # Not `a or b`: an ElementTree element with no children is falsy.
    seg_list = _child(rep, 'SegmentList')
    if seg_list is None:
        seg_list = _child(adaptation, 'SegmentList')
    if seg_list is not None:
        init = _child(seg_list, 'Initialization')
        if init is not None:
            add(urljoin(base, init.attrib.get('sourceURL', '')) if init.attrib.get('sourceURL') else base,
                _parse_range(init.attrib.get('range')))
        for s in _children(seg_list, 'SegmentURL'):
            add(urljoin(base, s.attrib['media']) if s.attrib.get('media') else base,
                _parse_range(s.attrib.get('mediaRange')))
        if not jobs:
            raise DashError('SegmentList 內沒有片段')
        return jobs

    # SegmentBase, or nothing at all: the BaseURL is the whole track as one file.
    add(base)
    return jobs


def _kind(adaptation, rep) -> str:
    for el in (rep, adaptation):
        ct = el.attrib.get('contentType') or ''
        mime = el.attrib.get('mimeType') or ''
        if ct in ('video', 'audio', 'text'):
            return ct
        if mime.startswith(('video/', 'audio/', 'text/')):
            return mime.split('/', 1)[0]
        if mime == 'application/mp4' or 'ttml' in mime or 'vtt' in mime:
            return 'text'
    codecs = rep.attrib.get('codecs') or adaptation.attrib.get('codecs') or ''
    if codecs.startswith(('mp4a', 'ac-3', 'ec-3', 'opus', 'vorbis', 'flac')):
        return 'audio'
    if codecs:
        return 'video'
    return 'unknown'


def plan(manifest_text: str, manifest_url: str) -> dict:
    """Pick the best video and audio and list each track's segments.

    Returns ``{'video': [jobs], 'audio': [jobs] | None, 'video_info': str}``.
    """
    try:
        mpd = ET.fromstring(manifest_text)
    except ET.ParseError as e:
        raise DashError(f'無法解析 MPD: {e}')
    if _local(mpd.tag) != 'MPD':
        raise DashError('不是 MPD 資訊清單')
    if (mpd.attrib.get('type') or 'static') == 'dynamic':
        raise DashError('這是直播串流（dynamic MPD），無法下載')

    periods = _children(mpd, 'Period')
    if not periods:
        raise DashError('MPD 內沒有 Period')
    # Multi-period manifests are almost always an ad break stitched in front of
    # or around the programme; the longest period is the programme.
    total = parse_duration(mpd.attrib.get('mediaPresentationDuration'))

    def period_len(p):
        return parse_duration(p.attrib.get('duration')) or (total if len(periods) == 1 else 0)

    period = max(periods, key=period_len)
    period_duration = period_len(period) or total

    best = {}
    for adaptation in _children(period, 'AdaptationSet'):
        for rep in _children(adaptation, 'Representation'):
            kind = _kind(adaptation, rep)
            if kind not in ('video', 'audio'):
                continue
            protected = _children(adaptation, 'ContentProtection') or _children(rep, 'ContentProtection')
            height = int(rep.attrib.get('height') or adaptation.attrib.get('height') or 0)
            bandwidth = int(rep.attrib.get('bandwidth') or 0)
            score = (height, bandwidth) if kind == 'video' else (bandwidth,)
            if kind not in best or score > best[kind][0]:
                best[kind] = (score, adaptation, rep, bool(protected))

    if 'video' not in best:
        raise DashError('MPD 內找不到影片軌')
    if best['video'][3] or ('audio' in best and best['audio'][3]):
        raise DashError('此影片受 DRM 保護，無法下載')

    out = {}
    for kind in ('video', 'audio'):
        if kind not in best:
            out[kind] = None
            continue
        _, adaptation, rep, _ = best[kind]
        out[kind] = _track_jobs(rep, adaptation, period, mpd, manifest_url, period_duration, kind[0])
    _, _, vrep, _ = best['video']
    out['video_info'] = f'{vrep.attrib.get("width", "?")}x{vrep.attrib.get("height", "?")}, ' \
                        f'{int(vrep.attrib.get("bandwidth") or 0) / 1_000_000:.1f} Mbps'
    return out


def download(manifest_url: str, folder: str, out_path: str, headers=None, cookies=None,
             progress_cb=None) -> None:
    """Download the manifest's best video (+ audio) and mux into `out_path`.

    `progress_cb(done, total)` counts segments across both tracks.
    """
    response = requests.get(manifest_url, headers=headers, cookies=cookies, timeout=20)
    response.raise_for_status()
    # A redirect moves the base every relative segment URL resolves against.
    p = plan(response.text, response.url or manifest_url)
    print(f'DASH: 選擇 {p["video_info"]}，影片 {len(p["video"])} 段'
          + (f'，音訊 {len(p["audio"])} 段' if p['audio'] else '，無獨立音訊'))

    jobs = p['video'] + (p['audio'] or [])
    segments.fetch_all(jobs, folder, headers=headers, cookies=cookies, progress_cb=progress_cb)

    video_file = os.path.join(folder, 'track.video.mp4')
    segments.concat(folder, [j['name'] for j in p['video']], video_file)
    inputs = ['-i', video_file]
    audio_file = None
    if p['audio']:
        audio_file = os.path.join(folder, 'track.audio.mp4')
        segments.concat(folder, [j['name'] for j in p['audio']], audio_file)
        inputs += ['-i', audio_file]

    mux(inputs, out_path, bool(audio_file))
    for j in jobs:
        try:
            os.remove(os.path.join(folder, j['name']))
        except OSError:
            pass
    for f in (video_file, audio_file):
        if f:
            try:
                os.remove(f)
            except OSError:
                pass


def mux(inputs: list[str], out_path: str, two_tracks: bool) -> None:
    """Stream-copy one or two inputs into an MP4 (no re-encode). Raises on
    failure: the caller would otherwise promote a missing or half-written file."""
    maps = ['-map', '0:v:0', '-map', '1:a:0'] if two_tracks else ['-map', '0']
    tmp = out_path + '.mux.mp4'
    cmd = ['ffmpeg', '-y', '-loglevel', 'error', *inputs, *maps, '-c', 'copy',
           '-movflags', '+faststart', tmp]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise IOError(f'FFmpeg 合併失敗: {(result.stderr or "").strip()[-300:]}')
    os.replace(tmp, out_path)
