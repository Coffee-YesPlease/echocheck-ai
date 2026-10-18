"""Deployment settings. All production integrations are opt-in."""
from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent

@dataclass
class Settings:
    db_path: Path = ROOT / 'work' / 'echocheck-v02.db'
    audio_dir: Path = ROOT / 'work' / 'private-audio'
    app_origin: str = 'http://localhost:8000'
    mode: str = 'development'
    release_enabled: bool = False
    consent_version: str = '2026-10-18-v1'
    session_seconds: int = 28800
    recording_retention_seconds: int = 604800
    ticket_seconds: int = 300
    processing_seconds: int = 180
    secure_cookies: bool | None = None

    def __post_init__(self):
        self.db_path = Path(self.db_path)
        self.audio_dir = Path(self.audio_dir)
        self.app_origin = self.app_origin.rstrip('/')
        url = urlsplit(self.app_origin)
        if url.scheme not in ('http', 'https') or not url.netloc or url.path or url.query or url.fragment or url.username:
            raise ValueError('APP_ORIGIN must be a complete http(s) origin without a path or credentials')
        if self.mode not in ('development', 'production', 'test', 'prototype'):
            raise ValueError('Mode must be development, production, test, or prototype')
        if min(self.session_seconds, self.recording_retention_seconds, self.ticket_seconds, self.processing_seconds) <= 0:
            raise ValueError('Expiry settings must be positive')
        if self.secure_cookies is None:
            self.secure_cookies = url.scheme == 'https'
        if self.mode == 'production' and (url.scheme != 'https' or not self.secure_cookies):
            raise ValueError('Production requires an HTTPS origin and secure cookies')

    def origin_allowed(self, value):
        if value == self.app_origin:
            return True
        if self.mode not in ('development','prototype') or not value:
            return False
        try:
            actual, configured = urlsplit(value), urlsplit(self.app_origin)
            loopback = {'localhost','127.0.0.1','::1'}
            return bool(actual.scheme == configured.scheme and actual.scheme in ('http','https')
                        and actual.hostname in loopback and configured.hostname in loopback
                        and (actual.port or (443 if actual.scheme=='https' else 80)) == (configured.port or (443 if configured.scheme=='https' else 80))
                        and not actual.username and not actual.password and not actual.path and not actual.query and not actual.fragment)
        except ValueError:
            return False

    @property
    def release_environment_ready(self):
        return self.mode == 'production' and self.release_enabled and self.app_origin.startswith('https://') and self.secure_cookies

    @classmethod
    def from_env(cls):
        return cls(
            db_path=Path(os.environ.get('ECHOCHECK_DB', str(ROOT / 'work' / 'echocheck-v02.db'))),
            audio_dir=Path(os.environ.get('ECHOCHECK_AUDIO_DIR', str(ROOT / 'work' / 'private-audio'))),
            app_origin=os.environ.get('APP_ORIGIN', 'http://localhost:8000'),
            mode=os.environ.get('ECHOCHECK_MODE', 'prototype' if (ROOT / '.prototype-enabled').is_file() else 'development'),
            release_enabled=os.environ.get('RELEASE_ENABLED', '').lower() == 'true',
            consent_version=os.environ.get('CONSENT_VERSION', '2026-10-18-v1'),
            session_seconds=int(os.environ.get('SESSION_SECONDS', '28800')),
            recording_retention_seconds=int(os.environ.get('RECORDING_RETENTION_SECONDS', '604800')),
            ticket_seconds=int(os.environ.get('RECORDING_TICKET_SECONDS', '300')),
            processing_seconds=int(os.environ.get('RECORDING_PROCESSING_SECONDS', '180')),
        )
