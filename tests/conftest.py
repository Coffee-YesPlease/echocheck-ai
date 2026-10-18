"""Keep the import-time application away from real data and live services.

backend.main builds a module-level app from the process environment when it
is imported. Without this, every test run would open work/echocheck-v02.db.
"""
import os
import tempfile

_ISOLATED = tempfile.mkdtemp(prefix='echocheck-tests-')
os.environ['ECHOCHECK_DB'] = os.path.join(_ISOLATED, 'import.db')
os.environ['ECHOCHECK_AUDIO_DIR'] = os.path.join(_ISOLATED, 'private-audio')
for name in ('PROVIDER_PROCESSING_ENABLED', 'FIRESTORE_MIRROR_ENABLED'):
    os.environ[name] = 'false'
