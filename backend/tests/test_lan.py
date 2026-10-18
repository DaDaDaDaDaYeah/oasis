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


# --- Docker / headless: same-host CORS ---------------------------------------

@pytest.mark.parametrize('origin, host', [
    ('http://192.168.1.50:3000', '192.168.1.50:8000'),
    ('http://nas:3000', 'nas:8000'),
    ('http://NAS.local:3000', 'nas.local:8000'),
    ('http://localhost:3000', 'localhost:8000'),
    ('http://[fd00::5]:3000', '[fd00::5]:8000'),
    ('http://192.168.1.50', '192.168.1.50:8000'),
])
def test_same_host_origin_is_allowed(origin, host):
    assert lan.origin_matches_host(origin, host)


@pytest.mark.parametrize('origin, host', [
    ('http://192.168.1.99:3000', '192.168.1.50:8000'),     # another device's page
    ('http://evil.example.com:3000', 'evil.example.com:8000'),  # DNS-rebinding shape
    ('http://nas:3000', 'nas.evil.com:8000'),
    ('ftp://nas:3000', 'nas:8000'),
    ('http://nas:3000/path', 'nas:8000'),
    ('null', 'nas:8000'),
    ('', 'nas:8000'),
    ('http://nas:3000', ''),
])
def test_other_origins_are_not_allowed(origin, host):
    assert not lan.origin_matches_host(origin, host)


def _cors_app():
    from fastapi import FastAPI
    import api
    app = FastAPI()
    app.add_middleware(api._SameHostCORS, allow_origins=['https://oasis.fusion-labs.cc'],
                       allow_methods=['*'], allow_headers=['*'])

    @app.get('/ping')
    def ping():
        return {'ok': True}

    return app


def test_middleware_adds_cors_headers_for_same_host_only():
    from fastapi.testclient import TestClient
    c = TestClient(_cors_app(), base_url='http://192.168.1.50:8000')
    ok = c.get('/ping', headers={'Origin': 'http://192.168.1.50:3000'})
    assert ok.headers.get('access-control-allow-origin') == 'http://192.168.1.50:3000'
    other = c.get('/ping', headers={'Origin': 'http://192.168.1.99:3000'})
    assert 'access-control-allow-origin' not in other.headers
    listed = c.get('/ping', headers={'Origin': 'https://oasis.fusion-labs.cc'})
    assert listed.headers.get('access-control-allow-origin') == 'https://oasis.fusion-labs.cc'


def test_middleware_answers_preflight_for_same_host():
    from fastapi.testclient import TestClient
    c = TestClient(_cors_app(), base_url='http://nas:8000')
    r = c.options('/ping', headers={'Origin': 'http://nas:3000',
                                    'Access-Control-Request-Method': 'GET',
                                    'Access-Control-Request-Headers': 'x-oasis-client'})
    assert r.status_code == 200 and r.headers['access-control-allow-origin'] == 'http://nas:3000'


def test_same_host_cors_is_off_by_default(monkeypatch):
    assert not lan.same_host_cors_enabled()
    monkeypatch.setenv('OASIS_CORS_SAME_HOST', '1')
    assert lan.same_host_cors_enabled()


# --- Chrome paths from the environment (containers) --------------------------

def test_chrome_service_is_default_when_nothing_is_configured(monkeypatch):
    import site_config
    monkeypatch.delenv('CHROMEDRIVER_PATH', raising=False)
    assert site_config.chrome_service() is None


def test_chrome_service_honours_chromedriver_path(monkeypatch):
    import site_config
    monkeypatch.setenv('CHROMEDRIVER_PATH', '/usr/bin/chromedriver')
    svc = site_config.chrome_service()
    assert svc is not None and svc.path == '/usr/bin/chromedriver'


def test_chrome_bin_sets_the_browser_binary(monkeypatch):
    import site_config
    from selenium.webdriver.chrome.options import Options
    monkeypatch.setenv('CHROME_BIN', '/usr/bin/chromium')
    opts = site_config.setup_driver_for_site(Options(), {})
    assert opts.binary_location == '/usr/bin/chromium'
    monkeypatch.delenv('CHROME_BIN')
    assert not site_config.setup_driver_for_site(Options(), {}).binary_location
