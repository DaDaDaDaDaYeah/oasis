"""LAN-mode helpers: bind host selection, the CORS regex, and the startup help."""

import re

import pytest

import lan


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv('HOST', raising=False)
    monkeypatch.delenv('OASIS_LAN', raising=False)


def test_default_is_loopback_only():
    assert lan.bind_host() == '127.0.0.1'
    assert not lan.lan_enabled()


def test_oasis_lan_binds_all_interfaces(monkeypatch):
    monkeypatch.setenv('OASIS_LAN', '1')
    assert lan.bind_host() == '0.0.0.0'
    assert lan.lan_enabled()


@pytest.mark.parametrize('value', ['0', 'false', '', 'no'])
def test_falsy_oasis_lan_stays_loopback(monkeypatch, value):
    monkeypatch.setenv('OASIS_LAN', value)
    assert lan.bind_host() == '127.0.0.1' and not lan.lan_enabled()


def test_explicit_host_wins_and_non_loopback_means_lan(monkeypatch):
    monkeypatch.setenv('HOST', '192.168.1.20')
    assert lan.bind_host() == '192.168.1.20' and lan.lan_enabled()


@pytest.mark.parametrize('host', ['127.0.0.1', 'localhost', '::1', '[::1]', '127.5.5.5'])
def test_explicit_loopback_host_is_not_lan(monkeypatch, host):
    monkeypatch.setenv('HOST', host)
    assert not lan.lan_enabled()


def test_lan_addresses_are_private_ipv4_only(monkeypatch):
    class FakeSock:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def connect(self, addr): pass
        def getsockname(self): return ('192.168.1.20', 5555)

    monkeypatch.setattr(lan.socket, 'socket', lambda *a, **k: FakeSock())
    monkeypatch.setattr(lan.socket, 'getaddrinfo', lambda *a, **k: [
        (2, 1, 6, '', ('127.0.0.1', 0)),        # loopback
        (2, 1, 6, '', ('169.254.9.9', 0)),      # link-local
        (2, 1, 6, '', ('8.8.8.8', 0)),          # public
        (2, 1, 6, '', ('10.0.0.7', 0)),         # private, second NIC
        (2, 1, 6, '', ('192.168.1.20', 0)),     # duplicate
    ])
    assert lan.lan_addresses() == ['192.168.1.20', '10.0.0.7']


def test_lan_addresses_survive_a_machine_with_no_network(monkeypatch):
    def boom(*a, **k):
        raise OSError('network unreachable')

    monkeypatch.setattr(lan.socket, 'socket', boom)
    monkeypatch.setattr(lan.socket, 'getaddrinfo', boom)
    assert lan.lan_addresses() == []


def test_origin_regex_admits_only_this_machines_own_addresses(monkeypatch):
    monkeypatch.setattr(lan, 'lan_addresses', lambda: ['192.168.1.20'])
    monkeypatch.setattr(lan.socket, 'gethostname', lambda: 'Desk-PC')
    rx = re.compile(lan.lan_origin_regex())
    for ok in ['http://192.168.1.20:3000', 'http://192.168.1.20',
               'http://desk-pc:3000', 'http://desk-pc.local:3000']:
        assert rx.match(ok), ok
    for bad in [
        'http://192.168.1.21:3000',          # another device on the LAN
        'http://192.168.1.20.evil.com:3000', # look-alike suffix
        'https://192.168.1.20:3000',         # wrong scheme
        'http://192.168.1.20:3000/x',        # not a bare origin
        'http://evil.com',
        'http://192x168y1z20:3000',          # dots must be literal
    ]:
        assert not rx.match(bad), bad


def test_origin_regex_is_none_when_nothing_is_known(monkeypatch):
    monkeypatch.setattr(lan, 'lan_addresses', lambda: [])
    monkeypatch.setattr(lan.socket, 'gethostname', lambda: '')
    assert lan.lan_origin_regex() is None


def test_describe_urls(monkeypatch):
    monkeypatch.setattr(lan, 'lan_addresses', lambda: ['192.168.1.20'])
    assert lan.describe_urls(8000, 3000) == [
        ('後端 API', 'http://192.168.1.20:8000'),
        ('網頁介面', 'http://192.168.1.20:3000'),
    ]
    assert lan.describe_urls(8000, None) == [('後端 API', 'http://192.168.1.20:8000')]


# --- wired into the app ------------------------------------------------------

def test_default_app_has_no_lan_cors_regex():
    import api
    assert api._LAN_ORIGIN_REGEX is None


def test_lan_help_is_silent_by_default(capsys):
    import api
    api._print_lan_help(8000)
    assert capsys.readouterr().out == ''


def test_lan_help_prints_urls_and_warns_when_remote_access_is_off(monkeypatch, capsys, auth_file):
    import api
    monkeypatch.setenv('OASIS_LAN', '1')
    monkeypatch.setattr(lan, 'lan_addresses', lambda: ['192.168.1.20'])
    monkeypatch.setenv('OASIS_FRONTEND_PORT', '3000')
    api._print_lan_help(8000)
    out = capsys.readouterr().out
    assert 'http://192.168.1.20:8000' in out and 'http://192.168.1.20:3000' in out
    assert '遠端存取尚未開啟' in out


def test_lan_help_explains_mixed_content_without_a_local_frontend(monkeypatch, capsys, auth_file):
    import api
    import auth
    auth.enable()
    monkeypatch.setenv('OASIS_LAN', '1')
    monkeypatch.delenv('OASIS_FRONTEND_PORT', raising=False)
    monkeypatch.setattr(lan, 'lan_addresses', lambda: ['192.168.1.20'])
    api._print_lan_help(8000)
    out = capsys.readouterr().out
    assert '遠端存取尚未開啟' not in out
    assert '混合內容' in out
