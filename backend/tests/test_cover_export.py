"""Writing a video's cached cover next to the video file (ABC-123.mp4 -> ABC-123.jpg)."""

import pytest

import catalog
from conftest import video

JPEG = b'\xff\xd8\xff\xe0fake-jpeg'
PNG = b'\x89PNG\r\n\x1a\nfake-png'


@pytest.fixture
def lib(db, tmp_path, monkeypatch):
    root = tmp_path / 'media'
    (root / 'movies').mkdir(parents=True)
    monkeypatch.setattr(catalog, 'MEDIA_ROOT', str(root))
    return root


def add(lib, code='ABC-123', *, cover=JPEG, mime='image/jpeg', file=True, path=None):
    rel = path or f'movies/{code}.mp4'
    if file:
        (lib / rel).parent.mkdir(parents=True, exist_ok=True)
        (lib / rel).write_bytes(b'video')
    catalog.insert_video(video(code, video_path=rel))
    vid = catalog.search_by_code(code)[0]['id']
    if cover is not None:
        _store_without_export(vid, cover, mime)
    return vid


def _store_without_export(vid, data, mime):
    """Put bytes in the DB the way store_cover_image does, minus the file export, so
    each test decides when the export happens."""
    import sqlite3
    conn = catalog.get_connection()
    conn.execute('UPDATE videos SET cover_image = ?, cover_type = ? WHERE id = ?',
                 (sqlite3.Binary(data), mime, vid))
    conn.commit()
    conn.close()


def test_writes_the_cover_beside_the_video_with_the_same_stem(lib):
    vid = add(lib)
    assert catalog.export_cover_file(vid) == 'written'
    assert (lib / 'movies' / 'ABC-123.jpg').read_bytes() == JPEG


@pytest.mark.parametrize('mime, ext', [
    ('image/jpeg', '.jpg'), ('image/png', '.png'), ('image/webp', '.webp'),
    ('image/gif', '.gif'), ('IMAGE/JPEG; charset=binary', '.jpg'),
])
def test_extension_follows_the_mime_type(lib, mime, ext):
    vid = add(lib, mime=mime)
    assert catalog.export_cover_file(vid) == 'written'
    assert (lib / 'movies' / f'ABC-123{ext}').exists()


def test_unknown_mime_is_skipped(lib):
    vid = add(lib, mime='image/x-weird')
    assert catalog.export_cover_file(vid) == 'unsupported'
    assert not list((lib / 'movies').glob('*.jpg'))


@pytest.mark.parametrize('existing', ['ABC-123.jpg', 'ABC-123.jpeg', 'ABC-123.png', 'ABC-123.webp'])
def test_never_overwrites_an_existing_image(lib, existing):
    vid = add(lib)
    (lib / 'movies' / existing).write_bytes(b'mine')
    assert catalog.export_cover_file(vid) == 'exists'
    assert (lib / 'movies' / existing).read_bytes() == b'mine'
    assert not (lib / 'movies' / 'ABC-123.jpg').exists() or existing == 'ABC-123.jpg'


def test_a_different_image_type_beside_the_video_also_counts_as_existing(lib):
    vid = add(lib, mime='image/jpeg')
    (lib / 'movies' / 'ABC-123.png').write_bytes(b'mine')
    assert catalog.export_cover_file(vid) == 'exists'
    assert not (lib / 'movies' / 'ABC-123.jpg').exists()


def test_running_twice_is_a_no_op(lib):
    vid = add(lib)
    assert catalog.export_cover_file(vid) == 'written'
    assert catalog.export_cover_file(vid) == 'exists'


def test_leaves_no_temp_files_behind(lib):
    vid = add(lib)
    catalog.export_cover_file(vid)
    assert sorted(p.name for p in (lib / 'movies').iterdir()) == ['ABC-123.jpg', 'ABC-123.mp4']


def test_no_cover_bytes_means_nothing_is_written_and_no_network_is_used(lib, monkeypatch):
    def boom(*a, **k):
        raise AssertionError('export must not touch the network')
    monkeypatch.setattr(catalog, 'fetch_cover_image', boom)
    vid = add(lib, cover=None)
    assert catalog.export_cover_file(vid) == 'no-cover'


def test_no_local_file_means_nothing_to_write(lib):
    vid = add(lib, file=False)
    assert catalog.export_cover_file(vid) == 'no-video'
    assert not list((lib / 'movies').iterdir())


def test_video_without_a_path(lib):
    catalog.insert_video(video('NOP-001'))
    vid = catalog.search_by_code('NOP-001')[0]['id']
    assert catalog.export_cover_file(vid) == 'no-video'


def test_unknown_video_id(lib):
    assert catalog.export_cover_file(9999) == 'no-video'


def test_path_traversal_in_video_path_is_refused(lib, tmp_path):
    outside = tmp_path / 'secret.mp4'
    outside.write_bytes(b'x')
    vid = add(lib, file=False, path='../secret.mp4')
    assert catalog.export_cover_file(vid) == 'no-video'
    assert not (tmp_path / 'secret.jpg').exists()


def test_symlink_out_of_media_root_is_refused(lib, tmp_path):
    outside = tmp_path / 'elsewhere'
    outside.mkdir()
    (outside / 'x.mp4').write_bytes(b'x')
    try:
        (lib / 'movies' / 'link.mp4').symlink_to(outside / 'x.mp4')
    except (OSError, NotImplementedError):
        pytest.skip('symlinks unavailable')
    vid = add(lib, 'LNK-001', file=False, path='movies/link.mp4')
    assert catalog.export_cover_file(vid) == 'no-video'
    assert not (outside / 'x.jpg').exists()


def test_falls_back_to_the_series_cover(lib):
    series = catalog.create_series('Show', None)
    sid = series['id'] if isinstance(series, dict) else series
    catalog.store_series_cover_image(sid, PNG, 'image/png')
    vid = add(lib, 'EP-001', cover=None)
    conn = catalog.get_connection()
    conn.execute('UPDATE videos SET series_id = ? WHERE id = ?', (sid, vid))
    conn.commit()
    conn.close()
    assert catalog.export_cover_file(vid) == 'written'
    assert (lib / 'movies' / 'EP-001.png').read_bytes() == PNG


def test_filesystem_without_hard_links_still_works(lib, monkeypatch):
    def nolink(*a, **k):
        raise OSError(1, 'Operation not permitted')
    monkeypatch.setattr(catalog.os, 'link', nolink)
    vid = add(lib)
    assert catalog.export_cover_file(vid) == 'written'
    assert (lib / 'movies' / 'ABC-123.jpg').read_bytes() == JPEG
    assert sorted(p.name for p in (lib / 'movies').iterdir()) == ['ABC-123.jpg', 'ABC-123.mp4']


def test_without_hard_links_an_existing_file_is_still_not_overwritten(lib, monkeypatch):
    monkeypatch.setattr(catalog.os, 'link', lambda *a, **k: (_ for _ in ()).throw(OSError(1, 'nope')))
    vid = add(lib)
    # appears between the check and the write
    real_exists = catalog.os.path.exists
    state = {'first': True}

    def exists(p):
        if state['first'] and str(p).endswith('.jpg'):
            state['first'] = False
            (lib / 'movies' / 'ABC-123.jpg').write_bytes(b'mine')
            return False
        return real_exists(p)
    monkeypatch.setattr(catalog.os.path, 'exists', exists)
    assert catalog.export_cover_file(vid) == 'exists'
    assert (lib / 'movies' / 'ABC-123.jpg').read_bytes() == b'mine'


def test_store_cover_image_exports_automatically(lib):
    vid = add(lib, cover=None)
    catalog.store_cover_image(vid, JPEG, 'image/jpeg')
    assert (lib / 'movies' / 'ABC-123.jpg').read_bytes() == JPEG


def test_store_cover_image_survives_an_export_failure(lib, monkeypatch):
    vid = add(lib, cover=None)
    monkeypatch.setattr(catalog, 'export_cover_file', lambda _id: (_ for _ in ()).throw(OSError('disk full')))
    catalog.store_cover_image(vid, JPEG, 'image/jpeg')           # must not raise
    assert catalog.get_cover_source(vid)['data'] == JPEG           # DB write still stands


def test_export_all_covers_reports_each_outcome(lib):
    add(lib, 'AAA-001')
    add(lib, 'BBB-002', cover=None)
    add(lib, 'CCC-003', file=False)
    done = add(lib, 'DDD-004')
    (lib / 'movies' / 'DDD-004.jpg').write_bytes(b'mine')
    catalog.insert_video(video('EEE-005'))                           # no path at all: not counted
    assert catalog.export_all_covers() == {'written': 1, 'no-cover': 1, 'no-video': 1, 'exists': 1}
    assert (lib / 'movies' / 'AAA-001.jpg').exists()
    assert done


def test_export_all_continues_past_one_failing_video(lib, monkeypatch):
    add(lib, 'AAA-001')
    add(lib, 'BBB-002')
    real = catalog.export_cover_file
    calls = []

    def flaky(vid):
        calls.append(vid)
        if len(calls) == 1:
            raise OSError('boom')
        return real(vid)
    monkeypatch.setattr(catalog, 'export_cover_file', flaky)
    counts = catalog.export_all_covers()
    assert counts == {'error': 1, 'written': 1}
