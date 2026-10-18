"""Shared fixtures for the backend test suite.

Several backend modules read their paths from the environment at *import* time
(auth.AUTH_PATH, catalog.DB_PATH, api.MEDIA_ROOT, site_config.ADAPTERS_DIR), so
this file points them at a throwaway directory before anything is imported, and
the per-test fixtures re-point the module attributes at a fresh directory so no
test can see another's database, adapters or auth file — and none can ever touch
a real oasis.db.
"""

import os
import sys
import tempfile

_SESSION_DIR = tempfile.mkdtemp(prefix='oasis-tests-')
os.environ['DB_PATH'] = os.path.join(_SESSION_DIR, 'session.db')
os.environ['OASIS_MEDIA_ROOT'] = os.path.join(_SESSION_DIR, 'media')
os.environ['OASIS_SITES_DIR'] = os.path.join(_SESSION_DIR, 'sites')

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import json  # noqa: E402

import pytest  # noqa: E402

import auth  # noqa: E402
import catalog  # noqa: E402
import db_setup  # noqa: E402
import site_config  # noqa: E402


@pytest.fixture
def workdir(tmp_path):
    return tmp_path


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A fresh, fully migrated SQLite database for one test."""
    path = str(tmp_path / 'oasis.db')
    monkeypatch.setattr(catalog, 'DB_PATH', path)
    monkeypatch.setattr(db_setup, 'DB_PATH', path)
    db_setup.create_tables()
    return path


@pytest.fixture
def auth_file(tmp_path, monkeypatch):
    """Isolated access-code file with a clean throttle table."""
    monkeypatch.setattr(auth, 'AUTH_PATH', str(tmp_path / 'oasis.auth.json'))
    auth._failures.clear()
    yield auth.AUTH_PATH
    auth._failures.clear()


@pytest.fixture
def sites_dir(tmp_path, monkeypatch):
    """An empty adapters directory; tests write the adapters they need."""
    d = tmp_path / 'sites'
    d.mkdir()
    monkeypatch.setattr(site_config, 'ADAPTERS_DIR', str(d))
    return d


def write_adapter(sites_dir, name, data):
    path = sites_dir / f'{name}.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


def video(code='ABC-123', **overrides):
    """A minimal valid record for catalog.insert_video."""
    record = {
        'code': code,
        'url': f'https://example.test/{code.lower()}',
        'title': f'title {code}',
        'title_zh_tw': f'title {code}',
        'actress': None,
        'tags': [],
        'cover': None,
        'video_path': None,
    }
    record.update(overrides)
    return record
