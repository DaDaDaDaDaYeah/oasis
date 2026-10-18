"""catalog.py: title parsing and translation handling (no database needed)."""

import pytest

import catalog


# --- extract_code ------------------------------------------------------------

@pytest.mark.parametrize('title, expected', [
    ('START-344 some title', 'START-344'),
    ('[SONE-228] title', 'SONE-228'),
    ('title ABF-362 more', 'ABF-362'),
    ('AB-12', 'AB-12'),
    ('ABCDEFGHIJ-12345', 'ABCDEFGHIJ-12345'),
    ('first AAA-111 then BBB-222', 'AAA-111'),
])
def test_extract_code_hits(title, expected):
    assert catalog.extract_code(title) == expected


@pytest.mark.parametrize('title', [
    'no code here',
    'lower-case-123',          # must be uppercase
    'A-123',                   # prefix too short
    'ABCDEFGHIJK-123',         # prefix too long
    'ABC-1',                   # too few digits
    'ABC-123456',              # too many digits
    'XABC-123x',               # letters glued onto either end: no \\b boundary
    '',
])
def test_extract_code_misses_or_boundaries(title):
    assert catalog.extract_code(title) is None


# --- extract_actress / extract_title -----------------------------------------

def test_extract_actress_from_dash_separated_title():
    assert catalog.extract_actress('START-344 Some Title - 三上悠亜', 'START-344') == '三上悠亜'


def test_extract_actress_from_last_space_segment():
    assert catalog.extract_actress('START-344 某個標題 三上悠亜', 'START-344') == '三上悠亜'


@pytest.mark.parametrize('title', [
    'START-344 plain english words',
    'START-344 標題一二三四五六七八九十十一',   # too long to be a name
    '',
])
def test_extract_actress_none_when_not_a_name(title):
    assert catalog.extract_actress(title, 'START-344') is None


@pytest.mark.parametrize('text, ok', [
    ('三上悠亜', True),
    ('みかみ', True),
    ('サンプル', True),
    ('A・B名', True),      # middle dot allowed
    ('名', False),         # too short
    ('一二三四五六七八九十一', False),   # too long
    ('abcdef', False),     # no CJK at all
    ('名abcdefg', False),  # too much noise
    ('', False),
])
def test_looks_like_name(text, ok):
    assert catalog._looks_like_name(text) is ok


def test_extract_title_strips_code_and_actress():
    assert catalog.extract_title('START-344 Some Title - 三上悠亜', 'START-344', '三上悠亜') == 'Some Title'


def test_extract_title_without_code_or_actress_is_unchanged():
    assert catalog.extract_title('Plain Title', None, None) == 'Plain Title'


def test_extract_title_only_removes_first_code_occurrence():
    assert catalog.extract_title('AAA-111 and AAA-111', 'AAA-111', None) == 'and AAA-111'


# --- extract_tags ------------------------------------------------------------

def test_extract_tags_dedups_sorts_and_drops_blanks():
    assert catalog.extract_tags(['b', 'a', 'b', '', None], None) == ['a', 'b']


def test_extract_tags_adds_studio_from_code(monkeypatch):
    assert catalog.extract_tags(['x'], 'START-344') == ['SODstar', 'x']
    assert catalog.extract_tags(['x'], 'ZZZZ-001') == ['x']


# --- script detection --------------------------------------------------------

@pytest.mark.parametrize('text, expected', [
    ('這是繁體中文標題', True),          # CJK, no kana, no simplified-only glyph
    ('这是简体中文标题', False),         # simplified-only glyphs
    ('日本語のタイトル', False),         # kana
    ('English only', False),            # no CJK
    ('', False),
    ('混合 mixed 繁體', True),
])
def test_is_zh_tw(text, expected):
    assert catalog.is_zh_tw(text) is expected


@pytest.mark.parametrize('text, expected', [
    ('ひらがな', True), ('カタカナ', True), ('漢字のみ', True),
    ('漢字', False), ('english', False), ('', False), (None, False),
])
def test_has_japanese_kana(text, expected):
    assert catalog.has_japanese_kana(text) is expected


# --- translation guards ------------------------------------------------------

@pytest.mark.parametrize('result, source, garbage', [
    ('', 'src', True),
    ("That's an error. Please try again later.", '日本語', True),
    ('That’s an error', '日本語', True),
    ('500 Server Error', '日本語', True),
    ('A normal translation', '日本語', False),
    # The source genuinely contains the phrase: not a failure.
    ("That's an error", "That's an error", False),
])
def test_is_translation_garbage(result, source, garbage):
    assert catalog._is_translation_garbage(result, source) is garbage


class FakeTranslator:
    """Stand-in for deep_translator.GoogleTranslator driven by a script of replies."""

    replies = []
    calls = 0

    def __init__(self, source='auto', target='zh-TW'):
        pass

    def translate(self, text):
        FakeTranslator.calls += 1
        reply = FakeTranslator.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def fake_translator(monkeypatch):
    import deep_translator
    FakeTranslator.replies = []
    FakeTranslator.calls = 0
    monkeypatch.setattr(deep_translator, 'GoogleTranslator', FakeTranslator)
    monkeypatch.setattr('time.sleep', lambda s: None)
    return FakeTranslator


def test_translate_returns_first_good_result(fake_translator):
    fake_translator.replies = ['你好']
    assert catalog.translate_to_zh_tw('こんにちは') == '你好'
    assert fake_translator.calls == 1


def test_translate_retries_after_exception_and_error_page(fake_translator):
    fake_translator.replies = [RuntimeError('boom'), "That's an error", '你好']
    assert catalog.translate_to_zh_tw('こんにちは') == '你好'
    assert fake_translator.calls == 3


def test_translate_gives_back_original_when_all_attempts_fail(fake_translator):
    """The caller must store the source, never an error page."""
    fake_translator.replies = [RuntimeError('a'), "That's an error", None]
    assert catalog.translate_to_zh_tw('こんにちは') == 'こんにちは'
    assert fake_translator.calls == catalog.TRANSLATE_ATTEMPTS


def test_translate_empty_text_makes_no_calls(fake_translator):
    assert catalog.translate_to_zh_tw('') == ''
    assert fake_translator.calls == 0


def test_translate_tags_passes_through_when_nothing_simplified(fake_translator):
    assert catalog.translate_tags_to_zh_tw(['plain', '繁體']) == ['plain', '繁體']
    assert fake_translator.calls == 0


def test_translate_tags_batches_simplified_ones_in_one_request(fake_translator):
    fake_translator.replies = ['電影\n視頻']
    out = catalog.translate_tags_to_zh_tw(['电影', 'keep', '影片', '视频'])
    # '影片' is valid Traditional, so only the two simplified tags are sent.
    assert out == ['電影', 'keep', '影片', '視頻']
    assert fake_translator.calls == 1


def test_translate_tags_falls_back_per_tag_when_lines_do_not_line_up(fake_translator):
    # Batch reply has the wrong number of lines, so each tag is translated alone.
    fake_translator.replies = ['only one line', '視頻', '電影']
    out = catalog.translate_tags_to_zh_tw(['视频', '电影'])
    assert out == ['視頻', '電影']
    assert fake_translator.calls == 3
