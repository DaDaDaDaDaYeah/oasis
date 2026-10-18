"""auth.py: the access code, request classification and the brute-force throttle.

This is the module whose whole job is to stay safe when someone tunnels the
backend to the internet, so the tests concentrate on the ways a request can
*look* local without being local.
"""

import json
import os
import stat
import sys
from types import SimpleNamespace

import pytest

import auth


def make_request(host='localhost:8000', peer='127.0.0.1', headers=None):
    """Just enough of a Starlette Request for auth.py: case-insensitive headers
    and a .client with a .host."""
    hdrs = {} if host is None else {'host': host}
    for k, v in (headers or {}).items():
        hdrs[k.lower()] = v
    client = None if peer is None else SimpleNamespace(host=peer)
    return SimpleNamespace(headers=_Headers(hdrs), client=client)


class _Headers(dict):
    def get(self, key, default=None):
        return super().get(key.lower(), default)

    def __contains__(self, key):
        return super().__contains__(key.lower())


# --- normalize ---------------------------------------------------------------

@pytest.mark.parametrize('raw, expected', [
    ('abcd-efgh', 'ABCDEFGH'),
    ('  ab cd  ef gh ', 'ABCDEFGH'),
    ('ABCD EFGH', 'ABCDEFGH'),
    ('', ''),
    (None, ''),
])
def test_normalize_is_cosmetic(raw, expected):
    assert auth.normalize(raw) == expected


# --- enable / disable / verify -----------------------------------------------

def test_off_by_default(auth_file):
    assert auth.get_code() is None
    assert auth.has_code() is False
    assert auth.verify('ANYTHING') is False


def test_enable_mints_well_formed_code(auth_file):
    code = auth.enable()
    left, right = code.split('-')
    assert len(left) == len(right) == 4
    assert set(left + right) <= set(auth._ALPHABET)
    # The alphabet drops the look-alikes people misread off a console window.
    assert not set('IO01') & set(left + right)
    assert auth.get_code() == code and auth.has_code()


def test_enable_rotates_and_invalidates_old_code(auth_file):
    first = auth.enable()
    second = auth.enable()
    assert first != second
    assert auth.verify(second) is True
    assert auth.verify(first) is False


def test_verify_accepts_sloppy_typing(auth_file):
    code = auth.enable()
    assert auth.verify(code.lower())
    assert auth.verify(code.replace('-', ' '))
    assert auth.verify(code.replace('-', ''))


def test_verify_rejects_wrong_and_empty(auth_file):
    auth.enable()
    assert auth.verify('AAAA-AAAA') is False
    assert auth.verify('') is False
    assert auth.verify(None) is False


def test_disable_deletes_code(auth_file):
    auth.enable()
    auth.disable()
    assert auth.get_code() is None
    assert auth.verify('anything') is False


def test_enable_and_disable_reset_throttle(auth_file):
    for _ in range(auth._FAIL_LIMIT + 2):
        auth.register_failure('k')
    assert auth.lockout_remaining('k') > 0
    auth.enable()
    assert auth.lockout_remaining('k') == 0
    for _ in range(auth._FAIL_LIMIT + 2):
        auth.register_failure('k')
    auth.disable()
    assert auth.lockout_remaining('k') == 0


@pytest.mark.skipif(sys.platform.startswith('win'), reason='POSIX modes only')
def test_auth_file_is_owner_only(auth_file):
    auth.enable()
    assert stat.S_IMODE(os.stat(auth_file).st_mode) == 0o600


@pytest.mark.parametrize('content', ['not json', '[]', '"str"', '{"code": 123}', '{"code": ""}'])
def test_corrupt_auth_file_means_off(auth_file, content):
    """A damaged file must fail closed: remote access off, never 'any code works'."""
    with open(auth_file, 'w', encoding='utf-8') as f:
        f.write(content)
    assert auth.get_code() is None
    assert auth.verify('whatever') is False


def test_auth_file_roundtrip_contents(auth_file):
    code = auth.enable()
    with open(auth_file, encoding='utf-8') as f:
        data = json.load(f)
    assert data['code'] == code and isinstance(data['updated_at'], int)


# --- is_local_request --------------------------------------------------------

@pytest.mark.parametrize('host', [
    'localhost', 'localhost:8000', 'LOCALHOST:8000', '127.0.0.1', '127.0.0.1:8000',
    '127.1.2.3:8000', '[::1]:8000', '[::1]', '0.0.0.0:8000',
])
def test_local_hosts_are_local(host):
    assert auth.is_local_request(make_request(host=host)) is True


@pytest.mark.parametrize('host', [
    'abc123.ngrok.io', 'abc.trycloudflare.com', 'example.com:8000', '192.168.1.5:8000',
    '10.0.0.2', 'localhost.evil.com', '127.0.0.1.evil.com', '',
])
def test_non_local_hosts_are_not_local(host):
    """Header-less tunnels (ssh -R, ngrok tcp) forward the client's own Host."""
    assert auth.is_local_request(make_request(host=host)) is False


def test_missing_host_is_not_local():
    assert auth.is_local_request(make_request(host=None)) is False


@pytest.mark.parametrize('header', sorted(auth._PROXY_HEADERS))
def test_any_proxy_header_defeats_local_trust(header):
    """A tunnel agent connects from 127.0.0.1 and may even keep Host: localhost;
    the headers it injects are what give it away."""
    req = make_request(host='localhost:8000', headers={header: 'x'})
    assert auth.is_local_request(req) is False


def test_proxy_header_check_is_case_insensitive():
    req = make_request(headers={'X-Forwarded-For': '1.2.3.4'})
    assert auth.is_local_request(req) is False


@pytest.mark.parametrize('peer', ['192.168.1.9', '8.8.8.8', '2001:db8::1', 'not-an-ip', '', None])
def test_non_loopback_peer_is_not_local(peer):
    assert auth.is_local_request(make_request(peer=peer)) is False


@pytest.mark.parametrize('peer', ['127.0.0.1', '127.9.9.9', '::1'])
def test_loopback_peers_are_local(peer):
    assert auth.is_local_request(make_request(peer=peer)) is True


# --- client_key / extract_code -----------------------------------------------

def test_client_key_uses_rightmost_forwarded_entry():
    """The rightmost entry is the one *our* nearest proxy wrote; the left ones
    are whatever the client claimed."""
    req = make_request(headers={'x-forwarded-for': '6.6.6.6, 7.7.7.7, 9.9.9.9'})
    assert auth.client_key(req) == '9.9.9.9'


def test_client_key_ignores_blank_forwarded_entries():
    req = make_request(headers={'x-forwarded-for': '1.1.1.1, , '})
    assert auth.client_key(req) == '1.1.1.1'


def test_client_key_falls_back_to_peer():
    assert auth.client_key(make_request(peer='127.0.0.1')) == '127.0.0.1'
    assert auth.client_key(make_request(peer=None)) == 'unknown'


@pytest.mark.parametrize('value, expected', [
    ('Bearer ABCD-EFGH', 'ABCD-EFGH'),
    ('bearer   ABCD-EFGH  ', 'ABCD-EFGH'),
    ('Bearer ', None),
    ('Basic abc', None),
    ('', None),
])
def test_extract_code(value, expected):
    req = make_request(headers={'authorization': value} if value else {})
    assert auth.extract_code(req) == expected


# --- throttle ----------------------------------------------------------------

def test_failures_under_limit_do_not_lock(auth_file):
    for _ in range(auth._FAIL_LIMIT):
        auth.register_failure('k')
    assert auth.lockout_remaining('k') == 0


def test_lockout_starts_after_limit_and_doubles(auth_file, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(auth.time, 'time', lambda: now[0])
    for _ in range(auth._FAIL_LIMIT):
        auth.register_failure('k')
    auth.register_failure('k')                       # first failure past the limit
    first = auth.lockout_remaining('k')
    assert first == auth._LOCK_BASE_S + 1            # int(5.0) + 1
    auth.register_failure('k')
    second = auth.lockout_remaining('k')
    assert second == auth._LOCK_BASE_S * 2 + 1
    auth.register_failure('k')
    assert auth.lockout_remaining('k') == auth._LOCK_BASE_S * 4 + 1


def test_lockout_is_capped(auth_file, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(auth.time, 'time', lambda: now[0])
    for _ in range(auth._FAIL_LIMIT + 30):
        auth.register_failure('k')
    assert auth.lockout_remaining('k') == auth._LOCK_MAX_S + 1


def test_lockout_expires(auth_file, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(auth.time, 'time', lambda: now[0])
    for _ in range(auth._FAIL_LIMIT + 1):
        auth.register_failure('k')
    assert auth.lockout_remaining('k') > 0
    now[0] += auth._LOCK_BASE_S + 1
    assert auth.lockout_remaining('k') == 0


def test_lockout_is_per_caller(auth_file):
    """One attacker locks only himself out."""
    for _ in range(auth._FAIL_LIMIT + 1):
        auth.register_failure('attacker')
    assert auth.lockout_remaining('attacker') > 0
    assert auth.lockout_remaining('owner') == 0


def test_clear_failures(auth_file):
    for _ in range(auth._FAIL_LIMIT + 1):
        auth.register_failure('k')
    auth.clear_failures('k')
    assert auth.lockout_remaining('k') == 0
    auth.clear_failures('never-seen')        # must not raise
