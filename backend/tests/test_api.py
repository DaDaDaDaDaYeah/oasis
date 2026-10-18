"""api.py through FastAPI's TestClient.

TestClient lets the test choose the peer address and the Host header, which is
exactly what the auth middleware looks at — so these tests can play the owner at
the machine, a phone on a tunnel, and a stranger who found the tunnel URL.

The app's startup hook is deliberately never run (no `with TestClient(...)`): it
opens a browser, prints banners and starts background loops.
"""

import os

import pytest
from fastapi.testclient import TestClient

import api
import auth
import catalog
from conftest import video

CLIENT_HDR = {'X-Oasis-Client': '1'}


class _Peer:
    """ASGI wrapper that sets the connection's peer address. This Starlette
    version's TestClient hard-codes the peer to "testclient"; auth.py judges
    callers by their peer address, so the tests need to choose it."""

    def __init__(self, app, host, port=50000):
        self.app, self.peer = app, (host, port)

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            scope = {**scope, 'client': self.peer}
        await self.app(scope, receive, send)


def peer_client(base_url, peer_host, headers=None, peer_port=50000, **kw):
    return TestClient(_Peer(api.app, peer_host, peer_port), base_url=base_url, headers=headers, **kw)


def make_client(kind='local', headers=CLIENT_HDR, **kw):
    """kind:
    local   – the owner's browser: loopback peer, Host: localhost.
    tunnel  – an HTTP tunnel (ngrok & co.): loopback peer, but it injects
              X-Forwarded-For and the Host is the tunnel's domain.
    sshfwd  – a header-less forward (ssh -R, tailscale funnel): loopback peer,
              no proxy headers, but the client's own Host (the tunnel domain).
    lan     – another device on the network hitting the port directly.
    """
    if kind == 'local':
        return peer_client('http://localhost:8000', '127.0.0.1', headers, **kw)
    if kind == 'tunnel':
        return peer_client('https://abc123.ngrok.io', '127.0.0.1',
                           {**(headers or {}), 'X-Forwarded-For': '203.0.113.7'}, **kw)
    if kind == 'sshfwd':
        return peer_client('https://phone.example.net', '127.0.0.1', headers, **kw)
    if kind == 'lan':
        return peer_client('http://192.168.1.20:8000', '192.168.1.50', headers, **kw)
    raise ValueError(kind)


@pytest.fixture(autouse=True)
def _isolated(db, auth_file, tmp_path, monkeypatch):
    root = tmp_path / 'media'
    (root / 'movies').mkdir(parents=True)
    monkeypatch.setattr(api, 'MEDIA_ROOT', str(root))
    monkeypatch.setattr(catalog, 'MEDIA_ROOT', str(root))
    monkeypatch.setattr(auth, 'print_code', lambda: None)          # keep the code off stdout
    monkeypatch.setattr(catalog, 'translate_to_zh_tw', lambda text, *a, **k: text)
    monkeypatch.setattr(catalog, 'fetch_cover_image', lambda *a, **k: None)   # never touch the network
    return root


@pytest.fixture
def media(_isolated):
    return _isolated


@pytest.fixture
def local():
    return make_client('local')


def add_video(code='ABC-123', **kw):
    return catalog.insert_video(video(code, **kw))


def media_file(media, rel='movies/ABC-123.mp4', data=b'0123456789' * 100):
    path = media / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return rel, data


# =============================================================================
# CSRF header / CORS
# =============================================================================

def test_api_call_without_client_header_is_refused():
    r = make_client('local', headers={}).get('/api/videos')
    assert r.status_code == 403 and 'detail' in r.json()


def test_health_also_needs_the_client_header():
    """The header is a CSRF guard on everything a browser can attach headers to,
    the open paths included."""
    assert make_client('local', headers={}).get('/api/health').status_code == 403


def test_stream_paths_are_exempt_from_the_header_because_img_and_video_cannot_send_it(media):
    vid = add_video()
    r = make_client('local', headers={}).get(f'/api/stream/cover/{vid}')
    assert r.status_code == 404          # reached the handler (no cover stored), not the 403 gate


def test_cors_preflight_is_answered_only_for_allowed_origins():
    c = make_client('local', headers={})
    ok = c.options('/api/videos', headers={
        'Origin': 'https://oasis.fusion-labs.cc',
        'Access-Control-Request-Method': 'GET',
        'Access-Control-Request-Headers': 'x-oasis-client',
    })
    assert ok.status_code == 200
    assert ok.headers['access-control-allow-origin'] == 'https://oasis.fusion-labs.cc'
    bad = c.options('/api/videos', headers={
        'Origin': 'https://evil.example',
        'Access-Control-Request-Method': 'GET',
        'Access-Control-Request-Headers': 'x-oasis-client',
    })
    assert bad.status_code == 400
    assert 'access-control-allow-origin' not in bad.headers


def test_default_allowed_origins_do_not_include_a_wildcard():
    assert '*' not in api.ALLOWED_ORIGINS
    assert 'https://oasis.fusion-labs.cc' in api.ALLOWED_ORIGINS


# =============================================================================
# Authentication: who gets in
# =============================================================================

def test_owner_at_the_machine_needs_no_code_even_with_remote_access_off(local):
    assert local.get('/api/videos').status_code == 200


def test_owner_still_needs_no_code_when_remote_access_is_on(local):
    auth.enable()
    assert local.get('/api/videos').status_code == 200


def test_health_reports_where_the_caller_stands(local):
    body = local.get('/api/health').json()
    assert body == {'status': 'ok', 'code_set': False, 'authenticated': True, 'local': True}


@pytest.mark.parametrize('kind', ['tunnel', 'sshfwd', 'lan'])
def test_remote_caller_is_refused_outright_while_the_switch_is_off(kind):
    """With no code set the backend cannot tell remote callers apart, so it
    refuses them all — tunnelling an un-configured backend leaks nothing."""
    c = make_client(kind)
    r = c.get('/api/videos')
    assert r.status_code == 403 and r.json()['code_required'] is True
    assert c.get('/api/export').status_code == 403
    assert c.delete('/api/videos/1').status_code == 403


@pytest.mark.parametrize('kind', ['tunnel', 'sshfwd', 'lan'])
def test_health_is_open_but_never_claims_a_remote_caller_is_local(kind):
    body = make_client(kind).get('/api/health').json()
    assert body['local'] is False and body['authenticated'] is False and body['code_set'] is False


@pytest.mark.parametrize('kind', ['tunnel', 'sshfwd', 'lan'])
def test_remote_caller_without_a_code_gets_401_once_switch_is_on(kind):
    auth.enable()
    r = make_client(kind).get('/api/videos')
    assert r.status_code == 401 and r.json()['auth_required'] is True


@pytest.mark.parametrize('kind', ['tunnel', 'sshfwd', 'lan'])
def test_remote_caller_with_the_right_code_is_let_in(kind):
    code = auth.enable()
    c = make_client(kind)
    r = c.get('/api/videos', headers={'Authorization': f'Bearer {code}'})
    assert r.status_code == 200
    # Typed sloppily off a console window.
    r = c.get('/api/videos', headers={'Authorization': f'Bearer {code.lower().replace("-", " ")}'})
    assert r.status_code == 200
    assert c.get('/api/health', headers={'Authorization': f'Bearer {code}'}).json()['authenticated'] is True


def test_wrong_code_is_401_and_not_authenticated():
    auth.enable()
    c = make_client('tunnel')
    r = c.get('/api/videos', headers={'Authorization': 'Bearer WRONG-CODE'})
    assert r.status_code == 401
    assert c.get('/api/health', headers={'Authorization': 'Bearer WRONG-CODE'}).json()['authenticated'] is False


def test_token_query_param_is_accepted_only_on_stream_paths(media):
    code = auth.enable()
    rel, data = media_file(media)
    vid = add_video(video_path=rel)
    c = make_client('tunnel')
    assert c.get(f'/api/stream/{vid}', params={'token': code}).status_code == 200
    # Anywhere else a credential in the URL is ignored.
    assert c.get('/api/videos', params={'token': code}).status_code == 401
    assert c.get(f'/api/videos/{vid}', params={'token': code}).status_code == 401


def test_stream_without_a_token_from_a_remote_caller_is_refused(media):
    auth.enable()
    rel, _ = media_file(media)
    vid = add_video(video_path=rel)
    assert make_client('tunnel').get(f'/api/stream/{vid}').status_code == 401


def test_header_code_wins_over_a_bad_token_param(media):
    code = auth.enable()
    rel, _ = media_file(media)
    vid = add_video(video_path=rel)
    r = make_client('tunnel').get(f'/api/stream/{vid}', params={'token': 'nope'},
                                  headers={'Authorization': f'Bearer {code}'})
    assert r.status_code == 200


def test_rotating_the_code_cuts_off_old_holders():
    old = auth.enable()
    c = make_client('tunnel')
    assert c.get('/api/videos', headers={'Authorization': f'Bearer {old}'}).status_code == 200
    auth.enable()
    assert c.get('/api/videos', headers={'Authorization': f'Bearer {old}'}).status_code == 401


def test_disabling_remote_access_refuses_a_previously_paired_device():
    code = auth.enable()
    c = make_client('tunnel')
    auth.disable()
    assert c.get('/api/videos', headers={'Authorization': f'Bearer {code}'}).status_code == 403


# =============================================================================
# Authentication: throttling
# =============================================================================

def test_repeated_wrong_codes_lock_the_caller_out_with_retry_after():
    auth.enable()
    c = make_client('tunnel')
    bad = {'Authorization': 'Bearer WRONG-CODE'}
    statuses = [c.get('/api/videos', headers=bad).status_code for _ in range(auth._FAIL_LIMIT + 1)]
    assert statuses[:auth._FAIL_LIMIT] == [401] * auth._FAIL_LIMIT
    assert statuses[-1] == 401
    locked = c.get('/api/videos', headers=bad)
    assert locked.status_code == 429 and int(locked.headers['Retry-After']) >= 1


def test_a_locked_caller_cannot_get_in_even_with_the_right_code():
    code = auth.enable()
    c = make_client('tunnel')
    for _ in range(auth._FAIL_LIMIT + 1):
        c.get('/api/videos', headers={'Authorization': 'Bearer WRONG-CODE'})
    assert c.get('/api/videos', headers={'Authorization': f'Bearer {code}'}).status_code == 429


def test_lockout_is_per_caller_so_an_attacker_cannot_lock_out_the_owner():
    code = auth.enable()
    attacker = peer_client('https://abc123.ngrok.io', '127.0.0.1',
                           {**CLIENT_HDR, 'X-Forwarded-For': '198.51.100.66'})
    phone = peer_client('https://abc123.ngrok.io', '127.0.0.1',
                        {**CLIENT_HDR, 'X-Forwarded-For': '203.0.113.7'})
    for _ in range(auth._FAIL_LIMIT + 2):
        attacker.get('/api/videos', headers={'Authorization': 'Bearer WRONG-CODE'})
    assert attacker.get('/api/videos').status_code == 429
    assert phone.get('/api/videos', headers={'Authorization': f'Bearer {code}'}).status_code == 200
    # …and the owner at the machine is never behind that bucket at all.
    assert make_client('local').get('/api/videos').status_code == 200


def test_spoofed_leftmost_forwarded_entry_does_not_dodge_the_throttle():
    """The tunnel appends the true client on the right; a rotating fake on the
    left must not buy an attacker fresh attempts."""
    auth.enable()
    statuses = []
    for i in range(auth._FAIL_LIMIT + 3):
        c = peer_client('https://abc123.ngrok.io', '127.0.0.1',
                        {**CLIENT_HDR, 'X-Forwarded-For': f'10.0.0.{i}, 203.0.113.9'}, peer_port=10 + i)
        statuses.append(c.get('/api/videos', headers={'Authorization': 'Bearer WRONG-CODE'}).status_code)
    assert 429 in statuses


def test_presenting_no_code_is_never_a_failure():
    """A phone whose code was rotated keeps polling; that must not be able to
    lock the owner's other devices (or itself) out."""
    code = auth.enable()
    c = make_client('tunnel')
    for _ in range(auth._FAIL_LIMIT * 4):
        assert c.get('/api/videos').status_code == 401
    assert c.get('/api/videos', headers={'Authorization': f'Bearer {code}'}).status_code == 200


def test_a_good_code_clears_earlier_failures():
    code = auth.enable()
    c = make_client('tunnel')
    for _ in range(auth._FAIL_LIMIT - 1):
        c.get('/api/videos', headers={'Authorization': 'Bearer WRONG-CODE'})
    assert c.get('/api/videos', headers={'Authorization': f'Bearer {code}'}).status_code == 200
    for _ in range(auth._FAIL_LIMIT - 1):
        c.get('/api/videos', headers={'Authorization': 'Bearer WRONG-CODE'})
    assert c.get('/api/videos', headers={'Authorization': f'Bearer {code}'}).status_code == 200


# =============================================================================
# Local-only endpoints
# =============================================================================

def test_open_in_player_is_refused_to_remote_callers_even_with_the_code(media):
    code = auth.enable()
    rel, _ = media_file(media)
    vid = add_video(video_path=rel)
    r = make_client('tunnel').post(f'/api/videos/{vid}/open', headers={'Authorization': f'Bearer {code}'})
    assert r.status_code == 403 and 'detail' in r.json()


def test_update_apply_is_refused_to_remote_callers_even_with_the_code(monkeypatch):
    import updater
    called = []
    monkeypatch.setattr(updater, 'apply_update', lambda: called.append(1) or {'status': 'updating'})
    code = auth.enable()
    r = make_client('tunnel').post('/api/update/apply', headers={'Authorization': f'Bearer {code}'})
    assert r.status_code == 403 and called == []


def test_update_apply_reaches_the_updater_for_the_owner(monkeypatch):
    import updater
    monkeypatch.setattr(updater, 'apply_update', lambda: {'status': 'updating'})
    assert make_client('local').post('/api/update/apply').json() == {'status': 'updating'}


def test_open_in_player_passes_the_gate_for_the_owner_and_launches_the_player(media, monkeypatch):
    import subprocess
    launched = []
    monkeypatch.setattr(subprocess, 'Popen', lambda cmd, *a, **k: launched.append(cmd))
    rel, _ = media_file(media)
    vid = add_video(video_path=rel)
    r = make_client('local').post(f'/api/videos/{vid}/open')
    assert r.status_code == 200 and r.json()['play_count'] == 1
    assert len(launched) == 1 and os.path.abspath(str(media / rel)) in launched[0]


def test_local_only_rule_matches_paths_precisely():
    assert api._is_local_only('/api/update/apply')
    assert api._is_local_only('/api/videos/7/open')
    assert not api._is_local_only('/api/videos/7')
    assert not api._is_local_only('/api/videos/7/play')
    assert not api._is_local_only('/api/update/check')
    assert not api._is_local_only('/api/series/open')        # only /api/videos/*/open launches a player


# =============================================================================
# Remote access switch and login
# =============================================================================

def test_login_when_remote_access_is_off():
    r = make_client('tunnel').post('/api/auth/login', json={'code': 'X'})
    # Remote + switch off: refused at the gate for non-open paths; login is open, so the handler answers.
    assert r.status_code == 400


def test_login_wrong_and_right_code():
    code = auth.enable()
    c = make_client('tunnel')
    assert c.post('/api/auth/login', json={'code': 'NOPE-NOPE'}).status_code == 401
    ok = c.post('/api/auth/login', json={'code': code.lower().replace('-', ' ')})
    assert ok.status_code == 200 and ok.json() == {'token': auth.normalize(code)}


def test_login_attempts_are_throttled_too():
    auth.enable()
    c = make_client('tunnel')
    codes = [c.post('/api/auth/login', json={'code': 'NOPE-NOPE'}).status_code for _ in range(auth._FAIL_LIMIT + 2)]
    assert 429 in codes


def test_owner_can_turn_remote_access_on_and_off(local):
    assert auth.has_code() is False
    assert local.post('/api/auth/remote').json() == {'code_set': True}
    assert auth.has_code() is True
    assert local.get('/api/health').json()['code_set'] is True
    assert local.delete('/api/auth/remote').json() == {'code_set': False}
    assert auth.has_code() is False


def test_a_stranger_cannot_claim_an_unclaimed_tunnel(media):
    """With the switch off no remote caller is authorised, so they never reach the
    endpoint that would mint a code."""
    for kind in ('tunnel', 'sshfwd', 'lan'):
        assert make_client(kind).post('/api/auth/remote').status_code == 403
    assert auth.has_code() is False


def test_the_code_is_never_in_a_response_body(local, monkeypatch):
    printed = []
    monkeypatch.setattr(auth, 'print_code', lambda: printed.append(auth.get_code()))
    enable = local.post('/api/auth/remote')
    reveal = local.post('/api/auth/reveal')
    code = auth.get_code()
    assert code and code not in enable.text and code not in reveal.text
    assert reveal.json() == {'status': 'printed'}
    assert printed == [code, code]            # it goes to the console instead


def test_reveal_requires_a_code_to_exist(local):
    assert local.post('/api/auth/reveal').status_code == 400


def test_turning_remote_access_on_rotates_the_code(local):
    local.post('/api/auth/remote')
    first = auth.get_code()
    local.post('/api/auth/remote')
    assert auth.get_code() != first


# =============================================================================
# Path traversal: video_path is DB-supplied
# =============================================================================

@pytest.fixture
def outside(tmp_path):
    """A secret file next to (not inside) the media root."""
    secret = tmp_path / 'secret.mp4'
    secret.write_bytes(b'TOP-SECRET')
    return secret


@pytest.fixture
def sibling_prefix(tmp_path):
    """A directory whose name merely *starts with* the media root's name."""
    d = tmp_path / 'media-evil'
    d.mkdir()
    f = d / 'x.mp4'
    f.write_bytes(b'EVIL')
    return f


def traversal_paths(outside, sibling_prefix):
    return [
        '../secret.mp4',
        '../../secret.mp4',
        'movies/../../secret.mp4',
        str(outside),                         # absolute path discards the root in os.path.join
        '../media-evil/x.mp4',                # shares the root's string prefix
        '/etc/passwd',
    ]


@pytest.mark.parametrize('idx', range(6))
def test_stream_refuses_paths_that_escape_the_media_root(media, outside, sibling_prefix, idx):
    path = traversal_paths(outside, sibling_prefix)[idx]
    vid = add_video(video_path=path)
    r = make_client('local').get(f'/api/stream/{vid}')
    assert r.status_code == 404
    assert b'TOP-SECRET' not in r.content and b'EVIL' not in r.content


@pytest.mark.parametrize('idx', range(6))
def test_open_refuses_paths_that_escape_the_media_root(media, outside, sibling_prefix, idx, monkeypatch):
    import subprocess
    launched = []
    monkeypatch.setattr(subprocess, 'Popen', lambda cmd, *a, **k: launched.append(cmd))
    vid = add_video(video_path=traversal_paths(outside, sibling_prefix)[idx])
    assert make_client('local').post(f'/api/videos/{vid}/open').status_code == 404
    assert launched == []


@pytest.mark.parametrize('idx', range(6))
def test_delete_never_removes_a_file_outside_the_media_root(media, outside, sibling_prefix, idx):
    vid = add_video(video_path=traversal_paths(outside, sibling_prefix)[idx])
    r = make_client('local').delete(f'/api/videos/{vid}')
    assert r.status_code == 200 and r.json()['deleted_file'] is False
    assert outside.exists() and sibling_prefix.exists()


def test_series_delete_with_videos_never_removes_files_outside_the_media_root(media, outside):
    s = catalog.create_series('S')
    inside_rel, _ = media_file(media, 'movies/in.mp4')
    add_video('AAA-001', video_path=inside_rel, series_id=s['id'], episode=1)
    add_video('AAA-002', video_path='../secret.mp4', series_id=s['id'], episode=2)
    r = make_client('local').delete(f"/api/series/{s['id']}", params={'delete_videos': 'true'})
    assert r.status_code == 200
    assert not (media / inside_rel).exists()          # inside the root: removed
    assert outside.exists()                            # outside: untouched
    assert catalog.list_all_videos() == []


def test_local_file_exists_flag_is_false_for_an_escaping_path(media, outside):
    vid = add_video(video_path='../secret.mp4')
    body = make_client('local').get(f'/api/videos/{vid}').json()
    assert body['local_file_exists'] is False and 'file_size' not in body or body.get('file_size') is None


def test_import_cannot_plant_a_path_that_streams_a_file(media, outside):
    c = make_client('local')
    r = c.post('/api/import', json={'videos': [
        {'code': 'EVL-001', 'url': 'u', 'title': 't', 'video_path': '../secret.mp4'},
    ]})
    assert r.json() == {'imported': 1, 'skipped': 0}
    vid = catalog.search_by_code('EVL-001')[0]['id']
    assert c.get(f'/api/stream/{vid}').status_code == 404


# =============================================================================
# Streaming
# =============================================================================

def test_stream_whole_file(media, local):
    rel, data = media_file(media)
    vid = add_video(video_path=rel)
    r = local.get(f'/api/stream/{vid}')
    assert r.status_code == 200 and r.content == data
    assert r.headers['accept-ranges'] == 'bytes' and r.headers['content-type'] == 'video/mp4'


@pytest.mark.parametrize('rng, start, end', [
    ('bytes=0-9', 0, 9),
    ('bytes=100-199', 100, 199),
    ('bytes=990-', 990, 999),
    ('bytes=0-0', 0, 0),
])
def test_stream_byte_ranges(media, local, rng, start, end):
    rel, data = media_file(media)
    vid = add_video(video_path=rel)
    r = local.get(f'/api/stream/{vid}', headers={'Range': rng})
    assert r.status_code == 206
    assert r.content == data[start:end + 1]
    assert r.headers['content-range'] == f'bytes {start}-{end}/{len(data)}'


def test_a_symlink_out_of_media_root_is_not_followed(media, local, tmp_path_factory):
    """A lexical prefix check passes movies/evil.mp4 even when it is a symlink to a
    file outside MEDIA_ROOT; stream, open-target and delete must all refuse it."""
    outside = tmp_path_factory.mktemp('outside') / 'secret.mp4'
    outside.write_bytes(b'top secret')
    link = media / 'movies' / 'evil.mp4'
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip('symlinks unavailable on this platform')
    vid = add_video(video_path='movies/evil.mp4')
    assert local.get(f'/api/stream/{vid}').status_code == 404
    assert local.delete(f'/api/videos/{vid}?local_only=true').status_code == 200
    assert outside.exists(), 'delete must not remove a file outside MEDIA_ROOT'


def test_stream_unknown_video_and_missing_file(media, local):
    assert local.get('/api/stream/999').status_code == 404
    vid = add_video(video_path='movies/gone.mp4')
    assert local.get(f'/api/stream/{vid}').status_code == 404


def test_stream_filename_is_url_encoded_in_the_header(media, local):
    rel, _ = media_file(media, 'movies/標題 ABC-123.mp4')
    vid = add_video(video_path=rel)
    disposition = local.get(f'/api/stream/{vid}').headers['content-disposition']
    assert "filename*=utf-8''" in disposition and '標' not in disposition


def test_stream_suffix_range_means_the_last_n_bytes(media, local):
    rel, data = media_file(media)
    vid = add_video(video_path=rel)
    r = local.get(f'/api/stream/{vid}', headers={'Range': 'bytes=-10'})
    assert r.status_code == 206 and r.content == data[-10:]
    assert r.headers['content-range'] == f'bytes {len(data) - 10}-{len(data) - 1}/{len(data)}'


def test_stream_suffix_longer_than_the_file_returns_the_whole_file(media, local):
    rel, data = media_file(media)
    vid = add_video(video_path=rel)
    r = local.get(f'/api/stream/{vid}', headers={'Range': f'bytes=-{len(data) + 500}'})
    assert r.status_code == 206 and r.content == data


def test_stream_end_past_eof_is_clamped(media, local):
    rel, data = media_file(media)
    vid = add_video(video_path=rel)
    r = local.get(f'/api/stream/{vid}', headers={'Range': f'bytes=990-{len(data) + 500}'})
    assert r.status_code == 206 and r.content == data[990:]
    assert r.headers['content-range'] == f'bytes 990-{len(data) - 1}/{len(data)}'


@pytest.mark.parametrize('rng', ['bytes={n}-', 'bytes={n}-{m}', 'bytes=-0'])
def test_stream_unsatisfiable_range_is_416(media, local, rng):
    rel, data = media_file(media)
    vid = add_video(video_path=rel)
    r = local.get(f'/api/stream/{vid}', headers={'Range': rng.format(n=len(data) + 50, m=len(data) + 60)})
    assert r.status_code == 416
    assert r.headers['content-range'] == f'bytes */{len(data)}'


@pytest.mark.parametrize('rng', [
    'bytes=abc-', 'bytes=-', 'bytes=5', 'bytes=9-3', 'bytes=--5', 'bytes=1-2-3',
    'items=0-9', 'garbage', 'bytes=0-9,20-29',
])
def test_stream_unusable_range_is_ignored_not_a_server_error(media, rng):
    """A header we cannot honour falls back to the whole file (RFC 9110), never a 500."""
    rel, data = media_file(media)
    vid = add_video(video_path=rel)
    c = make_client('local', raise_server_exceptions=False)
    r = c.get(f'/api/stream/{vid}', headers={'Range': rng})
    assert r.status_code == 200 and r.content == data


# =============================================================================
# Catalog endpoints
# =============================================================================

def test_manual_add_then_list_and_get(local):
    r = local.post('/api/videos', json={'url': 'https://x/1', 'title': '[START-344] Title - Jane',
                                        'actress': 'Jane', 'tags': ['a', 'b']})
    assert r.status_code == 200
    body = r.json()
    assert body['code'] == 'START-344' and body['title'] == 'Title'
    assert body['is_downloading'] is False and body['local_file_exists'] is False
    assert 'download_pending' not in body
    assert [v['code'] for v in local.get('/api/videos').json()] == ['START-344']
    assert local.get(f"/api/videos/{body['id']}").json()['tags'] == ['a', 'b']


def test_manual_add_duplicate_code_is_400_and_validation_is_422(local):
    local.post('/api/videos', json={'url': 'u', 'title': 'T', 'code': 'ABC-123'})
    assert local.post('/api/videos', json={'url': 'u2', 'title': 'T', 'code': 'abc-123'}).status_code == 400
    assert local.post('/api/videos', json={'url': 'u'}).status_code == 422
    assert local.post('/api/videos', json={'url': ' ', 'title': 'T'}).status_code == 400


def test_get_unknown_video_is_404(local):
    assert local.get('/api/videos/999').status_code == 404
    assert local.put('/api/videos/999/tags', json={'tags': []}).status_code == 404
    assert local.put('/api/videos/999', json={'title': 'x'}).status_code == 404
    assert local.post('/api/videos/999/play').status_code == 404
    assert local.delete('/api/videos/999').status_code == 404


def test_list_filters(local):
    add_video('AAA-001', actress='Jane Doe', tags=['x', 'y'])
    add_video('BBB-001', actress='John', tags=['y', 'z'])
    add_video('CCC-001', actress=None, tags=[])

    def codes(**params):
        return sorted(v['code'] for v in local.get('/api/videos', params=params).json())

    assert codes() == ['AAA-001', 'BBB-001', 'CCC-001']
    assert codes(actress='jane') == ['AAA-001']
    assert codes(code='bbb-001') == ['BBB-001']
    assert codes(tags='y') == ['AAA-001', 'BBB-001']
    assert codes(tags='x,y') == ['AAA-001']                       # all (default)
    assert codes(tags='x,z', match='any') == ['AAA-001', 'BBB-001']
    assert codes(tags='x,z') == []
    assert codes(actress='jane', tags='y') == ['AAA-001']


def test_list_filters_match_cjk_tags(local):
    add_video('AAA-001', tags=['劇情', '中文'])
    assert [v['code'] for v in local.get('/api/videos', params={'tags': '劇情'}).json()] == ['AAA-001']


def test_facets_count_and_order(local):
    add_video('AAA-001', actress='Jane', tags=['b', 'a'])
    add_video('BBB-001', actress='Jane', tags=['b'])
    add_video('CCC-001', actress='Amy', tags=['c'])
    body = local.get('/api/facets').json()
    assert body['actresses'] == [{'name': 'Jane', 'count': 2}, {'name': 'Amy', 'count': 1}]
    assert body['tags'] == [{'name': 'b', 'count': 2}, {'name': 'a', 'count': 1}, {'name': 'c', 'count': 1}]


def test_tags_and_details_updates(local):
    vid = add_video()
    assert local.put(f'/api/videos/{vid}/tags', json={'tags': [' a ', 'a', 'b']}).json()['tags'] == ['a', 'b']
    r = local.put(f'/api/videos/{vid}', json={'title': 'New', 'actress': 'Jane'})
    assert r.status_code == 200 and r.json()['title_zh_tw'] == 'New' and r.json()['actress'] == 'Jane'
    assert local.put(f'/api/videos/{vid}', json={'code': ''}).status_code == 400
    add_video('OTH-001')
    assert local.put(f'/api/videos/{vid}', json={'code': 'OTH-001'}).status_code == 400


def test_omitting_series_id_leaves_it_alone_but_null_removes_it(local):
    """None is a real value here; pydantic's fields_set is what tells the two apart."""
    s = catalog.create_series('S')
    vid = add_video(series_id=s['id'], episode=3)
    assert local.put(f'/api/videos/{vid}', json={'title': 'x'}).json()['series_id'] == s['id']
    body = local.put(f'/api/videos/{vid}', json={'series_id': None}).json()
    assert body['series_id'] is None and body['episode'] is None


def test_play_counter(local):
    vid = add_video()
    assert local.post(f'/api/videos/{vid}/play').json() == {'play_count': 1}
    assert local.post(f'/api/videos/{vid}/play').json() == {'play_count': 2}


def test_delete_video_removes_file_and_row(media, local):
    rel, _ = media_file(media)
    vid = add_video(video_path=rel)
    r = local.delete(f'/api/videos/{vid}')
    assert r.json() == {'status': 'deleted', 'deleted_file': True}
    assert not (media / rel).exists() and catalog.get_video_by_id(vid) is None


def test_delete_local_only_keeps_the_row_but_drops_file_and_path(media, local):
    rel, _ = media_file(media)
    vid = add_video(video_path=rel)
    r = local.delete(f'/api/videos/{vid}', params={'local_only': 'true'})
    assert r.json() == {'status': 'deleted_local', 'deleted_file': True}
    assert not (media / rel).exists()
    assert catalog.get_video_by_id(vid)['video_path'] is None


def test_delete_is_refused_while_downloading(media, local, monkeypatch):
    rel, _ = media_file(media)
    vid = add_video(video_path=rel)
    monkeypatch.setattr(api, '_is_downloading', lambda video_id: True)
    assert local.delete(f'/api/videos/{vid}').status_code == 409
    assert (media / rel).exists() and catalog.get_video_by_id(vid) is not None


def test_series_delete_with_videos_is_refused_while_any_is_downloading(local, monkeypatch):
    s = catalog.create_series('S')
    add_video(series_id=s['id'], episode=1)
    monkeypatch.setattr(api, '_is_downloading', lambda video_id: True)
    assert local.delete(f"/api/series/{s['id']}", params={'delete_videos': 'true'}).status_code == 409
    assert catalog.get_series_by_id(s['id']) is not None


# --- series ------------------------------------------------------------------

def test_series_lifecycle(local):
    created = local.post('/api/series', json={'name': ' Season 1 '}).json()
    assert created['name'] == 'Season 1'
    assert local.post('/api/series', json={'name': 'Season 1'}).json()['id'] == created['id']   # idempotent
    assert local.post('/api/series', json={'name': '  '}).status_code == 400

    ids = [add_video(f'AAA-{n:03d}') for n in range(1, 4)]
    r = local.post(f"/api/series/{created['id']}/videos", json={'video_ids': ids})
    assert r.json() == {'status': 'ok', 'assigned': 3}
    listed = local.get('/api/series').json()['series']
    assert [(s['name'], s['count']) for s in listed] == [('Season 1', 3)]

    renamed = local.put(f"/api/series/{created['id']}", json={'name': 'Renamed'})
    assert renamed.status_code == 200 and renamed.json()['name'] == 'Renamed'

    gone = local.delete(f"/api/series/{created['id']}")
    assert gone.json() == {'status': 'deleted', 'deleted_videos': False}
    assert len(catalog.list_all_videos()) == 3                              # videos survive


def test_series_error_paths(local):
    assert local.put('/api/series/999', json={'name': 'x'}).status_code == 404
    assert local.delete('/api/series/999').status_code == 404
    assert local.post('/api/series/999/videos', json={'video_ids': [1]}).status_code == 404
    a = catalog.create_series('A')
    catalog.create_series('B')
    assert local.put(f"/api/series/{a['id']}", json={'name': 'B'}).status_code == 400


def test_series_delete_with_videos_removes_them(local):
    s = catalog.create_series('S')
    add_video('AAA-001', series_id=s['id'], episode=1)
    add_video('BBB-001')
    r = local.delete(f"/api/series/{s['id']}", params={'delete_videos': 'true'})
    assert r.json() == {'status': 'deleted', 'deleted_videos': True}
    assert [v['code'] for v in catalog.list_all_videos()] == ['BBB-001']


# --- covers ------------------------------------------------------------------

def test_cover_served_from_db_with_immutable_caching(local):
    vid = add_video()
    catalog.store_cover_image(vid, b'\x89PNG-bytes', 'image/png')
    r = local.get(f'/api/stream/cover/{vid}')
    assert r.status_code == 200 and r.content == b'\x89PNG-bytes'
    assert r.headers['content-type'] == 'image/png'
    assert 'immutable' in r.headers['cache-control']


def test_cover_404s(local):
    assert local.get('/api/stream/cover/999').status_code == 404
    vid = add_video()                                   # exists but has no cover and no origin URL
    assert local.get(f'/api/stream/cover/{vid}').status_code == 404


def test_cover_is_fetched_once_then_cached(local, monkeypatch):
    vid = add_video(cover='https://origin.test/a.jpg')
    calls = []
    monkeypatch.setattr(catalog, 'fetch_cover_image',
                        lambda url, referer=None: calls.append((url, referer)) or (b'JPEG', 'image/jpeg'))
    assert local.get(f'/api/stream/cover/{vid}').content == b'JPEG'
    assert local.get(f'/api/stream/cover/{vid}').content == b'JPEG'
    assert len(calls) == 1 and calls[0][0] == 'https://origin.test/a.jpg'


# --- import / export ---------------------------------------------------------

def test_export_import_roundtrip_through_the_api(local):
    s = catalog.create_series('Season')
    add_video('AAA-001', tags=['t'], series_id=s['id'], episode=1)
    exported = local.get('/api/export').json()['videos']
    assert set(exported[0]) == set(catalog.EXPORT_FIELDS)
    for v in catalog.list_all_videos():
        catalog.delete_video(v['id'])
    assert local.post('/api/import', json={'videos': exported}).json() == {'imported': 1, 'skipped': 0}
    rec = local.get('/api/videos', params={'code': 'AAA-001'}).json()[0]
    assert rec['series'] == 'Season' and rec['episode'] == 1 and rec['tags'] == ['t']


def test_import_rejects_malformed_bodies(local):
    assert local.post('/api/import', json={'videos': 'nope'}).status_code == 422
    assert local.post('/api/import', json={}).status_code == 422
    assert local.post('/api/import', json={'videos': ['x']}).status_code == 422


# --- misc --------------------------------------------------------------------

def test_supported_sites_reflects_installed_adapters(local, sites_dir):
    from conftest import write_adapter
    write_adapter(sites_dir, 'ex', {'id': 'ex', 'name': 'Ex', 'domains': ['ex.tv']})
    body = local.get('/api/supported-sites').json()
    sites = body['sites'] if isinstance(body, dict) and 'sites' in body else body
    assert [s['id'] for s in sites] == ['ex']


def test_non_api_paths_are_not_gated_by_the_auth_middleware():
    """The middleware guards /api/*; anything else is FastAPI's own (docs, 404)."""
    auth.enable()
    assert make_client('tunnel').get('/definitely-not-a-route').status_code == 404
