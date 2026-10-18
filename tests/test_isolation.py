from pathlib import Path

from backend.config import ROOT
import backend.main


def test_import_time_app_never_uses_the_real_database():
    settings = backend.main.app.state.settings
    real_work = (ROOT / 'work').resolve()
    assert real_work not in settings.db_path.resolve().parents
    assert real_work not in Path(settings.audio_dir).resolve().parents
