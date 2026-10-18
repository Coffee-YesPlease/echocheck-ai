"""Container packaging keeps the prototype catalog, mode and origin consistent."""
from __future__ import annotations

from pathlib import Path
import re

import pytest

import backend.demo
from backend.config import Settings
from backend.main import create_app
from test_demo import ForbiddenLiveProvider

ROOT = Path(__file__).resolve().parents[1]


def lines(name):
    return [line.strip() for line in (ROOT / name).read_text().splitlines() if line.strip() and not line.startswith('#')]


def test_docker_context_and_image_include_only_the_practice_catalog():
    ignored = lines('.dockerignore')
    assert 'dataset' not in ignored and 'dataset/*' in ignored
    assert '!dataset/demo_scenarios_v1.json' in ignored
    dockerfile = (ROOT / 'Dockerfile').read_text()
    assert 'COPY dataset/demo_scenarios_v1.json ./dataset/demo_scenarios_v1.json' in dockerfile
    port = re.search(r'PORT=(\d+)', dockerfile).group(1)
    assert re.search(r'APP_ORIGIN=http://localhost:' + port + r'\b', dockerfile)


def test_cloud_upload_excludes_runtime_data_and_secrets():
    ignored = lines('.gcloudignore')
    for pattern in ('.venv/', '.env', 'work/', '*.db', '.git'):
        assert pattern in ignored


def test_prototype_without_catalog_fails_with_a_clear_message(tmp_path, monkeypatch):
    monkeypatch.setattr(backend.demo, 'ROOT', tmp_path)
    with pytest.raises(RuntimeError, match='dataset/demo_scenarios_v1.json'):
        create_app(settings=Settings(mode='prototype', db_path=tmp_path / 'p.db', audio_dir=tmp_path / 'a'), provider=ForbiddenLiveProvider())


def test_additional_origins_are_exact_and_https_in_production(tmp_path):
    settings = Settings(mode='prototype', app_origin='https://echocheck-a.run.app', extra_origins=('https://echocheck-b.run.app/',))
    assert settings.origin_allowed('https://echocheck-b.run.app')
    assert not settings.origin_allowed('https://echocheck-c.run.app')
    with pytest.raises(ValueError):
        Settings(mode='production', app_origin='https://a.example', extra_origins=('http://b.example',))
    with pytest.raises(ValueError):
        Settings(extra_origins=('https://b.example/path',))
