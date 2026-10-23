"""catalog.py against a real (temporary) SQLite database."""

import os

import pytest

import catalog
import db_setup
from conftest import video


@pytest.fixture(autouse=True)
def _db(db):
    """Every test in this module gets a fresh migrated database."""
    return db


@pytest.fixture
def media_root(tmp_path, monkeypatch):
    root = tmp_path / 'media'
    (root / 'movies').mkdir(parents=True)
    monkeypatch.setattr(catalog, 'MEDIA_ROOT', str(root))
    return root


# --- schema ------------------------------------------------------------------

def test_create_tables_is_idempotent(db):
    db_setup.create_tables()
    db_setup.create_tables()
    conn = catalog.get_connection()
    cols = {r['name'] for r in conn.execute('PRAGMA table_info(videos)')}
    conn.close()
    assert {'video_path', 'play_count', 'download_pending', 'cover_image', 'series_id', 'episode'} <= cols


def test_migrates_a_legacy_database(tmp_path, monkeypatch):
    """An old database with only the original columns must gain the new ones
    without losing rows."""
    import sqlite3
    path = str(tmp_path / 'legacy.db')
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE videos (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE, "
                 "url TEXT NOT NULL, title TEXT NOT NULL, title_zh_tw TEXT, actress TEXT, tags TEXT, "
                 "cover TEXT, created_at TEXT DEFAULT (datetime('now')))")
    conn.execute("INSERT INTO videos (code, url, title) VALUES ('OLD-001', 'u', 't')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(catalog, 'DB_PATH', path)
    monkeypatch.setattr(db_setup, 'DB_PATH', path)
    db_setup.create_tables()
    rec = catalog.search_by_code('old-001')[0]
    assert rec['code'] == 'OLD-001' and rec['series_id'] is None and rec['tags'] == []


# --- insert / upsert ---------------------------------------------------------

def test_insert_and_read_back():
    vid = catalog.insert_video(video('ABC-123', tags=['x', 'y'], actress='A'))
    rec = catalog.get_video_by_id(vid)
    assert rec['code'] == 'ABC-123' and rec['tags'] == ['x', 'y'] and rec['actress'] == 'A'
    assert rec['has_cover'] is False and rec['series'] is None


def test_get_video_by_id_missing():
    assert catalog.get_video_by_id(999) is None


def test_upsert_on_code_updates_instead_of_duplicating():
    first = catalog.insert_video(video('ABC-123', title='old'))
    second = catalog.insert_video(video('ABC-123', title='new'))
    assert first == second
    assert len(catalog.list_all_videos()) == 1
    assert catalog.get_video_by_id(first)['title'] == 'new'


def test_upsert_preserves_existing_video_path():
    vid = catalog.insert_video(video('ABC-123', video_path='movies/a.mp4'))
    catalog.insert_video(video('ABC-123', video_path=None, title='re-analysed'))
    rec = catalog.get_video_by_id(vid)
    assert rec['video_path'] == 'movies/a.mp4' and rec['title'] == 're-analysed'


def test_upsert_preserves_hand_set_series_unless_restated():
    series = catalog.create_series('Season')
    vid = catalog.insert_video(video('ABC-123', series_id=series['id'], episode=4))
    catalog.insert_video(video('ABC-123', title='again'))          # says nothing about series
    rec = catalog.get_video_by_id(vid)
    assert rec['series_id'] == series['id'] and rec['episode'] == 4
    other = catalog.create_series('Other')
    catalog.insert_video(video('ABC-123', series_id=other['id'], episode=1))
    rec = catalog.get_video_by_id(vid)
    assert rec['series'] == 'Other' and rec['episode'] == 1


def test_list_all_videos_newest_first_with_id_tiebreak():
    """created_at has one-second resolution, so a batch shares a timestamp and
    id has to break the tie."""
    ids = [catalog.insert_video(video(f'AAA-{n:03d}')) for n in range(1, 5)]
    assert [v['id'] for v in catalog.list_all_videos()] == sorted(ids, reverse=True)


def test_list_does_not_leak_cover_bytes():
    vid = catalog.insert_video(video('ABC-123'))
    catalog.store_cover_image(vid, b'\x89PNG...', 'image/png')
    rec = catalog.list_all_videos()[0]
    assert 'cover_image' not in rec and rec['has_cover'] is True


def test_search_by_code_uppercases_input():
    catalog.insert_video(video('ABC-123'))
    assert len(catalog.search_by_code('abc-123')) == 1
    assert catalog.search_by_code('ZZZ-999') == []


# --- delete / clear path / play count ----------------------------------------

def test_delete_video_returns_record_then_none():
    vid = catalog.insert_video(video('ABC-123'))
    assert catalog.delete_video(vid)['code'] == 'ABC-123'
    assert catalog.get_video_by_id(vid) is None
    assert catalog.delete_video(vid) is None


def test_clear_video_path():
    vid = catalog.insert_video(video('ABC-123', video_path='movies/a.mp4'))
    assert catalog.clear_video_path(vid)['video_path'] is None
    assert catalog.clear_video_path(999) is None


def test_increment_play_count():
    vid = catalog.insert_video(video('ABC-123'))
    assert catalog.increment_play_count(vid) == 1
    assert catalog.increment_play_count(vid) == 2
    assert catalog.increment_play_count(999) is None


# --- durable download queue --------------------------------------------------

def test_pending_downloads_survive_and_exclude_finished_ones():
    """The download_pending column is the queue's only durable state."""
    a = catalog.insert_video(video('AAA-001', url='https://x/a'))
    b = catalog.insert_video(video('BBB-001', url='https://x/b'))
    c = catalog.insert_video(video('CCC-001', url='https://x/c', video_path='movies/c.mp4'))
    for vid in (b, a, c):
        catalog.set_download_pending(vid, True)
    # c already has a local file, so it is not outstanding work; order is by id.
    assert catalog.get_pending_downloads() == [(a, 'https://x/a'), (b, 'https://x/b')]
    catalog.set_download_pending(a, False)
    assert catalog.get_pending_downloads() == [(b, 'https://x/b')]


def test_pending_ignores_empty_string_path_as_unfinished():
    vid = catalog.insert_video(video('AAA-001', video_path=''))
    catalog.set_download_pending(vid, True)
    assert [i for i, _ in catalog.get_pending_downloads()] == [vid]


# --- tags --------------------------------------------------------------------

def test_update_video_tags_trims_dedups_and_keeps_cjk():
    vid = catalog.insert_video(video('ABC-123'))
    rec = catalog.update_video_tags(vid, [' 劇情 ', '劇情', '', None, 'b'])
    assert rec['tags'] == ['劇情', 'b']
    assert catalog.update_video_tags(999, ['x']) is None


# --- update_video_details ----------------------------------------------------

def test_update_details_missing_video():
    assert catalog.update_video_details(999, title='x') is None


def test_update_details_no_changes_returns_row():
    vid = catalog.insert_video(video('ABC-123'))
    assert catalog.update_video_details(vid)['code'] == 'ABC-123'


def test_update_details_code_normalised_and_unique():
    a = catalog.insert_video(video('AAA-001'))
    catalog.insert_video(video('BBB-001'))
    assert catalog.update_video_details(a, code=' ccc-001 ')['code'] == 'CCC-001'
    with pytest.raises(ValueError):
        catalog.update_video_details(a, code='BBB-001')
    # Re-saving one's own code is not a clash.
    assert catalog.update_video_details(a, code='CCC-001')['code'] == 'CCC-001'


@pytest.mark.parametrize('field', ['code', 'title', 'url'])
def test_update_details_rejects_blank_required_fields(field):
    vid = catalog.insert_video(video('ABC-123'))
    with pytest.raises(ValueError):
        catalog.update_video_details(vid, **{field: '   '})


def test_update_title_syncs_original_only_when_they_matched():
    same = catalog.insert_video(video('AAA-001', title='t', title_zh_tw='t'))
    different = catalog.insert_video(video('BBB-001', title='日本語', title_zh_tw='中文'))
    r1 = catalog.update_video_details(same, title='new')
    r2 = catalog.update_video_details(different, title='new')
    assert (r1['title'], r1['title_zh_tw']) == ('new', 'new')
    assert (r2['title'], r2['title_zh_tw']) == ('日本語', 'new')


def test_changing_cover_url_drops_cached_image():
    vid = catalog.insert_video(video('ABC-123', cover='https://x/a.jpg'))
    catalog.store_cover_image(vid, b'bytes', 'image/jpeg')
    assert catalog.get_video_by_id(vid)['has_cover'] is True
    assert catalog.update_video_details(vid, cover='https://x/a.jpg')['has_cover'] is True   # unchanged
    assert catalog.update_video_details(vid, cover='https://x/b.jpg')['has_cover'] is False


def test_leaving_a_series_clears_the_episode():
    series = catalog.create_series('S')
    vid = catalog.insert_video(video('ABC-123', series_id=series['id'], episode=3))
    rec = catalog.update_video_details(vid, series_id=None)
    assert rec['series_id'] is None and rec['episode'] is None


def test_episode_is_ignored_without_a_series_and_series_must_exist():
    vid = catalog.insert_video(video('ABC-123'))
    assert catalog.update_video_details(vid, episode=5)['episode'] is None
    with pytest.raises(ValueError):
        catalog.update_video_details(vid, series_id=999)


# --- series ------------------------------------------------------------------

def test_create_series_is_idempotent_and_trims():
    a = catalog.create_series('  Season 1 ')
    b = catalog.create_series('Season 1')
    assert a['id'] == b['id'] and a['name'] == 'Season 1'
    assert len(catalog.list_series()) == 1


@pytest.mark.parametrize('name', ['', '   ', None])
def test_create_series_rejects_blank(name):
    with pytest.raises(ValueError):
        catalog.create_series(name)


def test_rename_series_conflict_and_blank():
    a = catalog.create_series('A')
    catalog.create_series('B')
    with pytest.raises(ValueError):
        catalog.rename_series(a['id'], 'B')
    with pytest.raises(ValueError):
        catalog.rename_series(a['id'], '  ')
    assert catalog.rename_series(a['id'], 'A2')['name'] == 'A2'
    assert catalog.rename_series(999, 'x') is None


def test_list_series_counts_members_largest_first():
    big = catalog.create_series('Big')
    small = catalog.create_series('Small')
    for n in range(3):
        catalog.insert_video(video(f'AAA-{n:03d}', series_id=big['id'], episode=n + 1))
    catalog.insert_video(video('ZZZ-001', series_id=small['id'], episode=1))
    listed = catalog.list_series()
    assert [(s['name'], s['count']) for s in listed] == [('Big', 3), ('Small', 1)]


def test_videos_in_series_are_in_episode_order():
    s = catalog.create_series('S')
    catalog.insert_video(video('AAA-003', series_id=s['id'], episode=3))
    catalog.insert_video(video('AAA-001', series_id=s['id'], episode=1))
    catalog.insert_video(video('AAA-002', series_id=s['id'], episode=2))
    assert [v['code'] for v in catalog.get_videos_by_series_id(s['id'])] == ['AAA-001', 'AAA-002', 'AAA-003']


def test_delete_series_keeps_videos_and_clears_dangling_refs():
    """No FOREIGN KEY exists, so delete_series has to unlink members itself."""
    s = catalog.create_series('S')
    vid = catalog.insert_video(video('AAA-001', series_id=s['id'], episode=2))
    assert catalog.delete_series(s['id']) is True
    rec = catalog.get_video_by_id(vid)
    assert rec is not None and rec['series_id'] is None and rec['episode'] is None
    assert catalog.get_series_by_id(s['id']) is None
    assert catalog.delete_series(s['id']) is False


def test_assign_numbers_continue_after_current_maximum():
    s = catalog.create_series('S')
    existing = catalog.insert_video(video('AAA-001', series_id=s['id'], episode=7))
    ids = [catalog.insert_video(video(f'BBB-{n:03d}')) for n in range(1, 4)]
    assert catalog.assign_videos_to_series(s['id'], ids) == 3
    eps = {v['id']: v['episode'] for v in catalog.get_videos_by_series_id(s['id'])}
    assert eps[existing] == 7
    assert [eps[i] for i in ids] == [8, 9, 10]


def test_assign_skips_already_placed_and_unknown_ids():
    s = catalog.create_series('S')
    placed = catalog.insert_video(video('AAA-001', series_id=s['id'], episode=1))
    new = catalog.insert_video(video('AAA-002'))
    assert catalog.assign_videos_to_series(s['id'], [placed, 9999, new]) == 1
    eps = {v['id']: v['episode'] for v in catalog.get_videos_by_series_id(s['id'])}
    assert eps == {placed: 1, new: 2}


def test_assign_moving_between_series_renumbers():
    a, b = catalog.create_series('A'), catalog.create_series('B')
    vid = catalog.insert_video(video('AAA-001', series_id=a['id'], episode=1))
    catalog.insert_video(video('BBB-001', series_id=b['id'], episode=5))
    assert catalog.assign_videos_to_series(b['id'], [vid]) == 1
    rec = catalog.get_video_by_id(vid)
    assert rec['series_id'] == b['id'] and rec['episode'] == 6


def test_assign_edge_cases():
    s = catalog.create_series('S')
    assert catalog.assign_videos_to_series(s['id'], []) == 0
    with pytest.raises(ValueError):
        catalog.assign_videos_to_series(999, [1])


def test_series_cover_copied_from_a_video_cover():
    s = catalog.create_series('S')
    vid = catalog.insert_video(video('AAA-001', cover='https://x/a.jpg'))
    catalog.store_cover_image(vid, b'img', 'image/png')
    updated = catalog.update_series(s['id'], cover=f'/api/stream/cover/{vid}')
    # Series rows report has_cover as a SQLite 0/1 (videos convert it to a bool).
    assert updated['cover'] == 'https://x/a.jpg' and bool(updated['has_cover']) is True


def test_series_cover_url_change_resets_cached_image():
    s = catalog.create_series('S', cover='https://x/a.jpg')
    catalog.store_series_cover_image(s['id'], b'img', 'image/png')
    assert bool(catalog.get_series_by_id(s['id'])['has_cover']) is True
    updated = catalog.update_series(s['id'], cover='https://x/b.jpg')
    assert updated['cover'] == 'https://x/b.jpg' and bool(updated['has_cover']) is False


# --- sequence codes ----------------------------------------------------------

SEQ = {'id': 'xvideos', 'code': {'from': 'sequence', 'prefix': 'XV', 'min_digits': 2, 'max_digits': 3}}


def test_sequence_first_codes_are_padded_and_match_code_pattern():
    code = catalog.next_adapter_sequence_code(SEQ, 'https://x/1')
    assert code == 'XV-01'
    assert catalog.CODE_PATTERN.search(code)


def test_sequence_continues_after_the_highest_number():
    catalog.insert_video(video('XV-07', url='https://x/7'))
    catalog.insert_video(video('XV-03', url='https://x/3'))
    assert catalog.next_adapter_sequence_code(SEQ, 'https://x/new') == 'XV-08'


def test_sequence_is_idempotent_per_url():
    """Re-analysing a video must reuse its number, not file a second row."""
    catalog.insert_video(video('XV-05', url='https://x/known'))
    catalog.insert_video(video('XV-06', url='https://x/other'))
    assert catalog.next_adapter_sequence_code(SEQ, 'https://x/known') == 'XV-05'


def test_sequence_ignores_non_matching_codes_with_same_prefix():
    catalog.insert_video(video('XV-AB', url='https://x/ab'))
    catalog.insert_video(video('XV-1-2', url='https://x/12'))
    assert catalog.next_adapter_sequence_code(SEQ, 'https://x/new') == 'XV-01'


def test_sequence_stops_at_max_digits_instead_of_widening():
    catalog.insert_video(video('XV-999', url='https://x/999'))
    assert catalog.next_adapter_sequence_code(SEQ, 'https://x/new') is None


def test_sequence_default_prefix_is_uppercased_adapter_id():
    adapter = {'id': 'site', 'code': {'from': 'sequence'}}
    assert catalog.next_adapter_sequence_code(adapter, 'u') == 'SITE-01'


@pytest.mark.parametrize('adapter', [{}, {'code': {}}, {'code': {'from': 'url'}}])
def test_sequence_none_when_adapter_does_not_ask(adapter):
    assert catalog.next_adapter_sequence_code(adapter, 'https://x/1') is None


def test_legacy_code_generators():
    assert catalog.next_youtube_code() == 'YT-1'
    catalog.insert_video(video('YT-4', url='https://y/4'))
    assert catalog.next_youtube_code() == 'YT-5'
    assert catalog.next_manual_code() == 'MANUAL-1'
    catalog.insert_video(video('MANUAL-9', url='https://m/9'))
    assert catalog.next_manual_code() == 'MANUAL-10'


# --- manual add --------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_translation(monkeypatch):
    """Manual add translates kana titles; keep the suite offline."""
    monkeypatch.setattr(catalog, 'translate_to_zh_tw', lambda text, *a, **k: f'zh:{text}')


def test_manual_add_extracts_code_and_cleans_title(media_root):
    rec = catalog.create_manual_video('https://x/1', '[START-344] Some Title - Jane', actress='Jane')
    assert rec['code'] == 'START-344' and rec['title'] == 'Some Title'
    assert catalog.get_video_by_id(rec['id'])['actress'] == 'Jane'


def test_manual_add_explicit_code_is_uppercased(media_root):
    assert catalog.create_manual_video('https://x/1', 'T', code=' abc-123 ')['code'] == 'ABC-123'


def test_manual_add_without_code_gets_placeholder(media_root):
    a = catalog.create_manual_video('https://x/1', 'no code at all')
    b = catalog.create_manual_video('https://x/2', 'still none')
    assert (a['code'], b['code']) == ('MANUAL-1', 'MANUAL-2')


def test_manual_add_rejects_duplicates_instead_of_overwriting(media_root):
    catalog.create_manual_video('https://x/1', 'T', code='ABC-123')
    with pytest.raises(ValueError):
        catalog.create_manual_video('https://x/2', 'other', code='ABC-123')
    assert catalog.search_by_code('ABC-123')[0]['url'] == 'https://x/1'


@pytest.mark.parametrize('url, title', [('', 'T'), ('  ', 'T'), ('https://x', ''), ('https://x', '  ')])
def test_manual_add_requires_url_and_title(media_root, url, title):
    with pytest.raises(ValueError):
        catalog.create_manual_video(url, title)


def test_manual_add_title_that_is_only_the_code_is_kept(media_root):
    assert catalog.create_manual_video('https://x/1', 'ABC-123')['title'] == 'ABC-123'


def test_manual_add_tags_accept_string_or_list_and_dedup(media_root):
    a = catalog.create_manual_video('https://x/1', 'T', code='AAA-001', tags='a, b ,a,,')
    b = catalog.create_manual_video('https://x/2', 'T', code='BBB-001', tags=['x', 'x', ' y'])
    assert catalog.get_video_by_id(a['id'])['tags'] == ['a', 'b']
    assert catalog.get_video_by_id(b['id'])['tags'] == ['x', 'y']


def test_manual_add_translates_only_japanese_titles(media_root):
    jp = catalog.create_manual_video('https://x/1', 'こんにちは', code='AAA-001')
    en = catalog.create_manual_video('https://x/2', 'hello', code='BBB-001')
    assert jp['title_zh_tw'] == 'zh:こんにちは' and en['title_zh_tw'] == 'hello'


def test_manual_add_finds_an_existing_download(media_root):
    (media_root / 'movies' / 'ABC-123 something.mp4').write_bytes(b'x')
    rec = catalog.create_manual_video('https://x/1', 'T', code='ABC-123')
    assert rec['video_path'] == os.path.join('movies', 'ABC-123 something.mp4')


# --- find_local_file ---------------------------------------------------------

def test_find_local_file_matches_code_anywhere_in_name(media_root):
    (media_root / 'movies' / '[tag] abc-123 Title.mp4').write_bytes(b'x')
    assert catalog.find_local_file('ABC-123', 'zzz') == os.path.join('movies', '[tag] abc-123 Title.mp4')


def test_find_local_file_looks_inside_folders_and_skips_non_mp4(media_root):
    folder = media_root / 'movies' / 'ABC-123'
    folder.mkdir()
    (folder / 'notes.txt').write_text('x')
    assert catalog.find_local_file('ABC-123', '') is None
    (folder / 'v.MP4').write_bytes(b'x')
    assert catalog.find_local_file('ABC-123', '') == os.path.join('movies', 'ABC-123', 'v.MP4')


def test_find_local_file_none_without_movies_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, 'MEDIA_ROOT', str(tmp_path / 'empty'))
    assert catalog.find_local_file('ABC-123', 'T') is None


# --- import / export ---------------------------------------------------------

def test_export_carries_only_portable_fields():
    s = catalog.create_series('Season')
    catalog.insert_video(video('AAA-001', video_path='movies/a.mp4', series_id=s['id'], episode=2))
    (rec,) = catalog.export_videos()
    assert set(rec) == set(catalog.EXPORT_FIELDS)
    assert rec['series'] == 'Season' and rec['episode'] == 2
    assert 'video_path' not in rec and 'id' not in rec and 'play_count' not in rec


def test_export_import_roundtrip_regroups_series_by_name(tmp_path, monkeypatch):
    s = catalog.create_series('Season')
    catalog.insert_video(video('AAA-001', series_id=s['id'], episode=1, tags=['t']))
    catalog.insert_video(video('AAA-002', series_id=s['id'], episode=2))
    exported = catalog.export_videos()

    other = str(tmp_path / 'other.db')
    monkeypatch.setattr(catalog, 'DB_PATH', other)
    monkeypatch.setattr(db_setup, 'DB_PATH', other)
    db_setup.create_tables()
    assert catalog.import_videos(exported) == {'imported': 2, 'skipped': 0}
    series = catalog.list_series()
    assert [(x['name'], x['count']) for x in series] == [('Season', 2)]
    assert [v['episode'] for v in catalog.get_videos_by_series_id(series[0]['id'])] == [1, 2]


def test_import_validates_and_normalises():
    result = catalog.import_videos([
        {'code': 'abc-123', 'url': ' https://x/1 ', 'title': ' T ', 'tags': 'a, b,a'},
        {'code': '', 'url': 'u', 'title': 't'},
        {'code': 'X-1', 'url': '', 'title': 't'},
        {'code': 'X-2', 'url': 'u', 'title': ''},
        'not a dict',
        None,
    ])
    assert result == {'imported': 1, 'skipped': 5}
    rec = catalog.search_by_code('ABC-123')[0]
    assert rec['url'] == 'https://x/1' and rec['title'] == 'T' and rec['tags'] == ['a', 'b']
    assert rec['title_zh_tw'] == 'T'


def test_import_rejects_non_list():
    with pytest.raises(ValueError):
        catalog.import_videos({'videos': []})


def test_import_never_sets_video_path_and_keeps_existing_one():
    """/api/import is reachable by an authorised caller; a crafted video_path in
    the payload must not reach the database."""
    vid = catalog.insert_video(video('ABC-123', video_path='movies/real.mp4'))
    catalog.import_videos([{'code': 'ABC-123', 'url': 'u', 'title': 't',
                            'video_path': '../../etc/passwd'},
                           {'code': 'NEW-001', 'url': 'u', 'title': 't',
                            'video_path': '../../etc/passwd'}])
    assert catalog.get_video_by_id(vid)['video_path'] == 'movies/real.mp4'
    assert catalog.search_by_code('NEW-001')[0]['video_path'] is None


@pytest.mark.parametrize('episode, expected', [('3', 3), (4, 4), ('', None), ('x', None), (None, None)])
def test_import_episode_parsing(episode, expected):
    catalog.import_videos([{'code': 'ABC-123', 'url': 'u', 'title': 't',
                            'series': 'S', 'episode': episode}])
    assert catalog.search_by_code('ABC-123')[0]['episode'] == expected


def test_import_drops_episode_without_series():
    catalog.import_videos([{'code': 'ABC-123', 'url': 'u', 'title': 't', 'episode': 5}])
    assert catalog.search_by_code('ABC-123')[0]['episode'] is None


# --- yt-dlp sites (X) -------------------------------------------------------------

def _fake_ytdlp(monkeypatch, info=None, error=None):
    import sys, types
    calls = []

    class FakeYDL:
        def __init__(self, opts):
            calls.append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            if error:
                raise error
            return info

    monkeypatch.setitem(sys.modules, 'yt_dlp', types.SimpleNamespace(YoutubeDL=FakeYDL))
    return calls


def test_x_post_is_analysed_through_ytdlp_without_selenium(db, sites_dir, monkeypatch):
    import shutil, os
    shutil.copy(os.path.join(os.path.dirname(catalog.__file__), 'sites', 'x.json'), sites_dir / 'x.json')
    calls = _fake_ytdlp(monkeypatch, {'title': 'Alice - look at this', 'thumbnail': 'https://img/t.jpg', 'id': '1'})
    monkeypatch.setattr(catalog, '_start_scrape_driver',
                        lambda *a: (_ for _ in ()).throw(AssertionError('Selenium must not start')))
    monkeypatch.setattr(catalog, 'translate_to_zh_tw', lambda t, *a, **k: t)
    monkeypatch.setattr(catalog, 'translate_tags_to_zh_tw', lambda t: t)
    monkeypatch.setattr(catalog, 'fetch_cover_image', lambda *a, **k: None)
    rec = catalog.process_url('https://x.com/alice/status/1234567890123456789', skip_download=True)
    assert rec['code'] == 'XP-01' and rec['cover'] == 'https://img/t.jpg'
    assert rec['actress'] is None            # "Alice - ..." must not be parsed as a cast name
    again = catalog.process_url('https://x.com/alice/status/1234567890123456789', skip_download=True)
    assert again['code'] == 'XP-01' and len(catalog.list_all_videos()) == 1
    assert calls[0]['noplaylist'] is True


def test_x_failure_is_reported_not_scraped(sites_dir, monkeypatch):
    import shutil, os
    shutil.copy(os.path.join(os.path.dirname(catalog.__file__), 'sites', 'x.json'), sites_dir / 'x.json')
    _fake_ytdlp(monkeypatch, error=RuntimeError('This tweet is NSFW; log in'))
    with pytest.raises(ValueError, match='NSFW'):
        catalog.fetch_page_metadata('https://twitter.com/a/status/1')


def test_multi_video_post_uses_first_entry(sites_dir, monkeypatch):
    import shutil, os
    shutil.copy(os.path.join(os.path.dirname(catalog.__file__), 'sites', 'x.json'), sites_dir / 'x.json')
    _fake_ytdlp(monkeypatch, {'entries': [None, {'title': 'first', 'id': '9', 'thumbnail': 't'}, {'title': 'second'}]})
    assert catalog.fetch_page_metadata('https://x.com/a/status/1')[0] == 'first'


def test_ytdlp_options_read_cookies_file(tmp_path, monkeypatch):
    import site_config
    monkeypatch.setenv('OASIS_MEDIA_ROOT', str(tmp_path))
    monkeypatch.delenv('OASIS_COOKIES_FILE', raising=False)
    assert 'cookiefile' not in site_config.ytdlp_options()
    (tmp_path / 'cookies.txt').write_text('# Netscape HTTP Cookie File\n')
    assert site_config.ytdlp_options()['cookiefile'] == str(tmp_path / 'cookies.txt')
    other = tmp_path / 'other.txt'
    other.write_text('x')
    monkeypatch.setenv('OASIS_COOKIES_FILE', str(other))
    assert site_config.ytdlp_options(quiet=False)['cookiefile'] == str(other)
    assert site_config.ytdlp_options(quiet=False)['quiet'] is False
