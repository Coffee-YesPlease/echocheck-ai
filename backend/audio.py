"""Conservative audio decoding and private storage."""
from __future__ import annotations
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import wave
from .store import uid

MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_AUDIO_SECONDS = 120
ALLOWED_MIME = {'audio/wav','audio/x-wav','audio/wave','audio/webm','audio/ogg','audio/mp4'}

class InvalidAudio(ValueError):
    pass

def validate_audio(data: bytes, mime: str):
    if mime not in ALLOWED_MIME:
        raise InvalidAudio('Use WAV, WebM, Ogg, or MP4 audio.')
    if not data or len(data) > MAX_AUDIO_BYTES:
        raise InvalidAudio('Recording must contain audio and be no larger than 8 MiB.')
    if mime in ('audio/wav','audio/x-wav','audio/wave'):
        try:
            with wave.open(io.BytesIO(data), 'rb') as audio:
                frames, rate, channels, width = audio.getnframes(), audio.getframerate(), audio.getnchannels(), audio.getsampwidth()
                if not 1 <= channels <= 2 or not 8000 <= rate <= 192000 or width not in (1,2,3,4) or frames < rate // 10 or frames > rate * MAX_AUDIO_SECONDS:
                    raise InvalidAudio('Use a recording between 0.1 and 120 seconds with one or two audio channels.')
                if len(audio.readframes(frames)) != frames * channels * width:
                    raise InvalidAudio('Recording is incomplete. Record again.')
        except (wave.Error, EOFError, ValueError) as exc:
            if isinstance(exc, InvalidAudio):
                raise
            raise InvalidAudio('This is not a complete PCM WAV recording. Record again.') from None
    else:
        probe, decoder = shutil.which('ffprobe'), shutil.which('ffmpeg')
        if not probe or not decoder:
            raise InvalidAudio('This server needs FFmpeg to validate compressed audio. Use PCM WAV or configure FFmpeg.')
        if (mime == 'audio/webm' and not data.startswith(b'\x1a\x45\xdf\xa3')) or (mime == 'audio/ogg' and not data.startswith(b'OggS')) or (mime == 'audio/mp4' and (len(data)<12 or data[4:8] != b'ftyp')):
            raise InvalidAudio('The recording format does not match its audio type. Record again.')
        suffix = {'audio/webm':'.webm','audio/ogg':'.ogg','audio/mp4':'.m4a'}[mime]
        demuxer = {'audio/webm':'matroska','audio/ogg':'ogg','audio/mp4':'mov'}[mime]
        with tempfile.NamedTemporaryFile(suffix=suffix) as file:
            file.write(data); file.flush()
            try:
                result = subprocess.run([probe,'-v','error','-protocol_whitelist','file,pipe','-show_streams','-show_format','-of','json','-f',demuxer,file.name], capture_output=True, timeout=12, check=True)
                details = json.loads(result.stdout)
                streams = details.get('streams', [])
                if len(streams) != 1 or streams[0].get('codec_type') != 'audio' or streams[0].get('channels') not in (1,2):
                    raise InvalidAudio('Recording must contain one audio stream and no video.')
                # Browser WebM commonly omits duration. Decode to bounded mono PCM instead.
                decoded = subprocess.run([decoder,'-nostdin','-v','error','-xerror','-protocol_whitelist','file,pipe','-f',demuxer,'-i',file.name,'-map','0:a:0','-t',str(MAX_AUDIO_SECONDS+1),'-ac','1','-ar','8000','-f','s16le','pipe:1'],capture_output=True,timeout=15,check=True)
                duration = len(decoded.stdout) / 16000
                if decoded.stderr or not 0.1 <= duration <= MAX_AUDIO_SECONDS:
                    raise InvalidAudio('Use a complete recording between 0.1 and 120 seconds.')
            except (subprocess.SubprocessError, ValueError, KeyError, OSError):
                raise InvalidAudio('Recording could not be decoded completely. Record again.') from None
    return hashlib.sha256(data).hexdigest()

def save_private_audio(directory: Path, data: bytes):
    filename = uid() + '.audio'
    path = directory / filename
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as file:
            file.write(data)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return filename
