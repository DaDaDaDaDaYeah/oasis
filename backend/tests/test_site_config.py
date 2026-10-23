"""site_config.py: adapter loading, host matching and the pure helpers.

detect_site() is the gate that decides which sites a headless browser may be
pointed at, so look-alike hosts are tested most heavily.
"""

import json

import pytest

import site_config
from conftest import write_adapter

EXAMPLE = {'id': 'example', 'name': 'Example', 'domains': ['example.tv']}
ROTATING = {'id': 'rot', 'name': 'Rotating', 'domain_prefixes': ['examplecdn']}


# --- load_adapters -----------------------------------------------------------

def test_missing_dir_gives_no_adapters(tmp_path, monkeypatch):
    monkeypatch.setattr(site_config, 'ADAPTERS_DIR', str(tmp_path / 'nope'))
    assert site_config.load_adapters() == []


def test_loads_valid_adapters_sorted(sites_dir):
    write_adapter(sites_dir, 'b', {'id': 'b', 'name': 'B', 'domains': ['b.tv']})
    write_adapter(sites_dir, 'a', {'id': 'a', 'name': 'A', 'domains': ['a.tv']})
    assert [a['id'] for a in site_config.load_adapters()] == ['a', 'b']


def test_skips_example_non_json_and_invalid(sites_dir):
    write_adapter(sites_dir, 'good', EXAMPLE)
    write_adapter(sites_dir, 'sites.example', {'id': 'x', 'name': 'X'})
    (sites_dir / 'notes.txt').write_text('hello')
    (sites_dir / 'broken.json').write_text('{ not json')
    write_adapter(sites_dir, 'no-id', {'name': 'No id'})
    write_adapter(sites_dir, 'no-name', {'id': 'x'})
    (sites_dir / 'list.json').write_text(json.dumps([1, 2, 3]))
    assert [a['id'] for a in site_config.load_adapters()] == ['example']


# --- supported_sites ---------------------------------------------------------

def test_supported_sites_domain_and_category(sites_dir):
    write_adapter(sites_dir, 'a', {**EXAMPLE, 'display_domain': 'shown.tv'})
    write_adapter(sites_dir, 'b', {'id': 'b', 'name': 'B', 'domain_prefixes': ['bcdn']})
    write_adapter(sites_dir, 'c', {'id': 'anime1', 'name': 'Anime', 'domains': ['anime1.me']})
    write_adapter(sites_dir, 'd', {'id': 'd', 'name': 'D', 'domains': ['d.tv'], 'category': 'general'})
    by_id = {s['id']: s for s in site_config.supported_sites()}
    assert by_id['example']['domain'] == 'shown.tv'
    assert by_id['b']['domain'] == 'bcdn'
    assert by_id['example']['category'] == 'av'          # default
    assert by_id['anime1']['category'] == 'general'      # built-in exception
    assert by_id['d']['category'] == 'general'           # explicit wins


def test_supported_sites_empty_without_adapters(sites_dir):
    assert site_config.supported_sites() == []


# --- detect_site -------------------------------------------------------------

@pytest.mark.parametrize('url', [
    'https://example.tv/video/1',
    'https://www.example.tv/video/1',
    'https://a.b.example.tv/x',
    'https://EXAMPLE.TV/x',
    'http://example.tv:8080/x',
])
def test_detect_site_matches_domain_and_subdomains(sites_dir, url):
    write_adapter(sites_dir, 'ex', EXAMPLE)
    assert site_config.detect_site(url)['id'] == 'example'


@pytest.mark.parametrize('url', [
    'https://example.tv.evil.com/x',        # the registrable domain is evil.com
    'https://evilexample.tv/x',             # suffix without a dot boundary
    'https://notexample.tv/x',
    'https://example.tv@evil.com/x',        # userinfo trick: host is evil.com
    'https://evil.com/?u=example.tv',
    'https://evil.com/example.tv',
    'https://example.com/x',
    'not a url',
    '',
])
def test_detect_site_rejects_lookalikes(sites_dir, url):
    write_adapter(sites_dir, 'ex', EXAMPLE)
    with pytest.raises(ValueError):
        site_config.detect_site(url)


def test_detect_site_domain_prefixes(sites_dir):
    write_adapter(sites_dir, 'rot', ROTATING)
    assert site_config.detect_site('https://examplecdn.com/x')['id'] == 'rot'
    assert site_config.detect_site('https://examplecdn9.ws/x')['id'] == 'rot'
    assert site_config.detect_site('https://www.examplecdn.xyz/x')['id'] == 'rot'
    with pytest.raises(ValueError):
        site_config.detect_site('https://xexamplecdn.com/x')


def test_detect_site_ignores_leading_dot_in_domain(sites_dir):
    write_adapter(sites_dir, 'ex', {**EXAMPLE, 'domains': ['.example.tv']})
    assert site_config.detect_site('https://example.tv/x')['id'] == 'example'


def test_detect_site_error_lists_supported(sites_dir):
    write_adapter(sites_dir, 'ex', EXAMPLE)
    with pytest.raises(ValueError) as exc:
        site_config.detect_site('https://other.org/x')
    assert 'other.org' in str(exc.value) and 'example.tv' in str(exc.value)


def test_detect_site_with_no_adapters_says_so(sites_dir):
    with pytest.raises(ValueError) as exc:
        site_config.detect_site('https://example.tv/x')
    assert 'sites.example.json' in str(exc.value)


# --- derive_code -------------------------------------------------------------

def test_derive_code_from_url_with_template():
    adapter = {'code': {'from': 'url', 'pattern': r'/watch/(\d+)/ep-(\d+)', 'template': 'SHOW-{1}-{2}'}}
    assert site_config.derive_code('https://x.tv/watch/12/ep-3', adapter) == 'SHOW-12-3'


def test_derive_code_default_template_is_first_group():
    adapter = {'code': {'pattern': r'id=(\d+)'}}
    assert site_config.derive_code('https://x.tv/?id=42', adapter) == '42'


def test_derive_code_from_title():
    adapter = {'code': {'from': 'title', 'pattern': r'第(\d+)集', 'template': 'EP-{1}'}}
    assert site_config.derive_code('https://x.tv/a', adapter, title='某動畫 第07集') == 'EP-07'


@pytest.mark.parametrize('adapter, url', [
    ({}, 'https://x.tv/1'),                                        # no code block
    ({'code': {}}, 'https://x.tv/1'),                              # no pattern
    ({'code': {'pattern': r'id=(\d+)'}}, 'https://x.tv/none'),     # no match
    ({'code': {'pattern': r'(x)?', 'template': ' '}}, 'https://x.tv/'),  # blank result
])
def test_derive_code_none_cases(adapter, url):
    assert site_config.derive_code(url, adapter) is None


def test_derive_code_unmatched_optional_group_is_empty():
    adapter = {'code': {'pattern': r'/a(\d+)(b)?', 'template': 'A{1}{2}'}}
    assert site_config.derive_code('https://x.tv/a5', adapter) == 'A5'


# --- listing helpers ---------------------------------------------------------

def test_is_listing_url():
    adapter = {'listing': {'url_patterns': [r'/category/', r'/playlist\?']}}
    assert site_config.is_listing_url('https://x.tv/category/2024/show', adapter)
    assert site_config.is_listing_url('https://x.tv/playlist?list=1', adapter)
    assert not site_config.is_listing_url('https://x.tv/12345', adapter)
    assert not site_config.is_listing_url('https://x.tv/category/', {})
    assert not site_config.is_listing_url('https://x.tv/category/', {'listing': {}})


@pytest.mark.parametrize('adapter, expected', [
    ({}, 1),
    ({'listing': {}}, 1),
    ({'listing': {'max_pages': 0}}, 1),
    ({'listing': {'max_pages': -4}}, 1),
    ({'listing': {'max_pages': None}}, 1),
    ({'listing': {'max_pages': 5}}, 5),
    ({'listing': {'max_pages': '3'}}, 3),
])
def test_listing_max_pages_is_a_hard_floor_of_one(adapter, expected):
    assert site_config.listing_max_pages(adapter) == expected


# --- media_mode / request headers -------------------------------------------

@pytest.mark.parametrize('adapter, expected', [
    ({}, 'hls'),
    ({'media': {}}, 'hls'),
    ({'media': {'mode': None}}, 'hls'),
    ({'media': {'mode': 'http'}}, 'http'),
    ({'media': {'mode': 'HTTP'}}, 'http'),
])
def test_media_mode(adapter, expected):
    assert site_config.media_mode(adapter) == expected


def test_request_headers_default_user_agent():
    headers = site_config.get_request_headers({})
    assert headers == {'User-Agent': site_config._DEFAULT_USER_AGENT}


def test_request_headers_substitute_page_url_and_override_ua():
    adapter = {'user_agent': 'UA/1', 'headers': {'Referer': '{page_url}', 'X-A': 'b'}}
    headers = site_config.get_request_headers(adapter, 'https://x.tv/v/1')
    assert headers == {'User-Agent': 'UA/1', 'Referer': 'https://x.tv/v/1', 'X-A': 'b'}


def test_adapter_header_can_replace_user_agent():
    adapter = {'headers': {'User-Agent': 'Custom'}}
    assert site_config.get_request_headers(adapter)['User-Agent'] == 'Custom'


# --- _json_path --------------------------------------------------------------

def test_json_path_walks_dicts_and_lists():
    data = {'a': {'b': [{'c': 'found'}, {'c': 'second'}]}}
    assert site_config._json_path(data, 'a.b.0.c') == 'found'
    assert site_config._json_path(data, 'a.b.1.c') == 'second'


@pytest.mark.parametrize('path', ['a.x', 'a.b.9', 'a.b.zero', 'q', 'a.b.0.c.d'])
def test_json_path_misses_are_none_not_errors(path):
    data = {'a': {'b': [{'c': 'found'}]}}
    assert site_config._json_path(data, path) is None


# --- _clean_filename / _match_first / build_ts_url ---------------------------

def test_clean_filename_strips_forbidden_characters():
    cleaned = site_config._clean_filename('a/b\\c:d*e?f"g<h>i|j')
    assert not set('/\\:*?"<>|') & set(cleaned)


def test_match_first_returns_first_hit_in_order():
    assert site_config._match_first([r'zzz(\d)', r'a(\d)', r'(\d)'], 'a7 9') == '7'
    assert site_config._match_first([r'nope'], 'a7') is None
    assert site_config._match_first([], 'a7') is None


def test_build_ts_url():
    assert site_config.build_ts_url('https://cdn.x/a.ts', 'https://base') == 'https://cdn.x/a.ts'
    assert site_config.build_ts_url('a.ts', 'https://base/path') == 'https://base/path/a.ts'


# --- scraping helpers against a fake Selenium driver --------------------------

class FakeElement:
    def __init__(self, text='', attrs=None):
        self.text = text
        self._attrs = attrs or {}

    def get_attribute(self, name):
        return self._attrs.get(name)


class FakeDriver:
    """Maps CSS selectors to elements; anything else raises, like Selenium does."""

    def __init__(self, elements=None, title=''):
        self._elements = elements or {}
        self.title = title

    def find_element(self, by, selector):
        found = self._elements.get(selector)
        if not found:
            raise LookupError(selector)
        return found[0]

    def find_elements(self, by, selector):
        return list(self._elements.get(selector, []))


def test_video_name_uses_first_nonblank_selector_and_cleans_it():
    driver = FakeDriver({'h1': [FakeElement('   ')], 'h2': [FakeElement('AB/C: title?')]}, title='ignored')
    adapter = {'title_selectors': ['missing', 'h1', 'h2']}
    assert site_config.get_video_name(driver, adapter) == 'ABC title'


def test_video_name_falls_back_to_page_title_minus_suffix():
    driver = FakeDriver(title='My Video - Example Site')
    adapter = {'title_selectors': ['h1'], 'title_suffix_strip': [' - Example Site']}
    assert site_config.get_video_name(driver, adapter) == 'My Video'


def test_video_name_last_resort_is_video():
    assert site_config.get_video_name(FakeDriver(title=''), {}) == 'video'


def test_actress_joins_unique_names():
    driver = FakeDriver({'.cast': [FakeElement('A'), FakeElement(' B '), FakeElement('A'), FakeElement('')]})
    assert site_config.get_video_actress(driver, {'actress_selectors': ['.cast', '.bad']}) == 'A、B'
    assert site_config.get_video_actress(driver, {}) is None


def test_tags_collect_nonblank_in_order():
    driver = FakeDriver({'.t': [FakeElement('x'), FakeElement(' '), FakeElement('y')]})
    assert site_config.get_video_tags(driver, {'tag_selectors': ['.t', '.nope']}) == ['x', 'y']


def test_cover_defaults_to_og_image_and_takes_first_url_in_value():
    og = {'meta[property="og:image"]': [FakeElement(attrs={'content': 'https://c.test/a.jpg'})]}
    assert site_config.get_cover_url(FakeDriver(og)) == 'https://c.test/a.jpg'
    styled = {'.hero': [FakeElement(attrs={'style': 'background-image: url(https://c.test/b.jpg)'})]}
    adapter = {'cover': {'selectors': [{'css': '.hero', 'attr': 'style'}]}}
    assert site_config.get_cover_url(FakeDriver(styled), adapter) == 'https://c.test/b.jpg'
    assert site_config.get_cover_url(FakeDriver(), adapter) is None


def test_missing_m3u8_reports_what_the_browser_saw(capsys):
    class Driver:
        current_url = 'https://example.test/cdn-cgi/challenge'
        title = 'Just a moment...'

        def find_element(self, by, sel):
            return type('E', (), {'text': 'Checking if the site connection is secure\n\nverify you are human'})()

        def execute_script(self, *a, **k):
            return ''

        page_source = ''

    with pytest.raises(ValueError) as e:
        site_config.get_m3u8_url(Driver(), {'id': 'x', 'name': 'X', 'm3u8': {'regexes': ['nomatch']}})
    shown = capsys.readouterr().out
    for needle in ('Just a moment', 'cdn-cgi/challenge', 'verify you are human'):
        assert needle in str(e.value) and needle in shown


def test_describe_page_never_raises():
    class Broken:
        @property
        def title(self):
            raise RuntimeError('chrome crashed')

        current_url = 'u'

        def find_element(self, *a):
            raise RuntimeError('gone')

    out = site_config.describe_page(Broken())
    assert "標題='?'" in out and "內容='?'" in out
