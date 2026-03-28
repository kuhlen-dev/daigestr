"""
Daigestr — Audio/Video Converter Utilities

Enthält Funktionen für Audio-Extraktion und Transkription:
- extract_audio_from_video: ffmpeg Audio-Extraktion
- transcribe_audio: faster-whisper Transkription
"""

import hashlib
import subprocess
from pathlib import Path
from typing import Any

import structlog

try:
    from faster_whisper import WhisperModel
    WHISPER_AVAILABLE = True
except ImportError:
    WHISPER_AVAILABLE = False

from settings import (
    TEMP_DIR,
    FFMPEG_TIMEOUT,
    WHISPER_MODEL_SIZE,
    WHISPER_DEVICE,
    WHISPER_COMPUTE_TYPE,
    _whisper_model_cache,
)
from utils import _get, _LOADED_BY_SERVER  # noqa: F401

log = structlog.get_logger()


def extract_audio_from_video(video_path: Path) -> Path:
    """Extrahiert den Audio-Track aus einer Video-Datei als WAV via ffmpeg (FR-MKIT-006).

    Args:
        video_path: Pfad zur Video-Datei.

    Returns:
        Pfad zur extrahierten WAV-Datei im TEMP_DIR.

    Raises:
        RuntimeError: Wenn ffmpeg fehlschlägt oder nicht installiert ist.
    """
    _temp_dir = _get("TEMP_DIR", TEMP_DIR)
    _ffmpeg_timeout = _get("FFMPEG_TIMEOUT", FFMPEG_TIMEOUT)
    wav_filename = f"{video_path.stem}_{hashlib.md5(str(video_path).encode()).hexdigest()[:8]}.wav"
    wav_path = _temp_dir / wav_filename

    log.info("extract_audio_from_video_start", video=str(video_path), output=str(wav_path))

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(video_path),
                "-vn",               # kein Video
                "-acodec", "pcm_s16le",
                "-ar", "16000",      # 16kHz — optimal für Whisper
                "-ac", "1",          # Mono
                str(wav_path),
            ],
            capture_output=True,
            text=True,
            timeout=_ffmpeg_timeout,
        )
        if result.returncode != 0:
            log.error(
                "extract_audio_from_video_failed",
                video=str(video_path),
                returncode=result.returncode,
                stderr=result.stderr,
            )
            raise RuntimeError(f"ffmpeg fehlgeschlagen (returncode={result.returncode}): {result.stderr}")
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg ist nicht installiert") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("ffmpeg-Timeout beim Audio-Extrahieren") from exc

    log.info("extract_audio_from_video_done", wav=str(wav_path))
    return wav_path


def transcribe_audio(audio_path: Path) -> dict[str, Any]:
    """Transkribiert eine Audio-Datei mit faster-whisper (FR-MKIT-006).

    Nutzt WHISPER_MODEL_SIZE aus der Umgebungsvariable.
    Das Modell wird gecacht — nach dem ersten Laden wird es wiederverwendet.

    Args:
        audio_path: Pfad zur Audio-Datei (WAV bevorzugt).

    Returns:
        Dict mit:
        - success (bool)
        - text (str): Vollständiges Transkript
        - language (str): Erkannte Sprache (z.B. "de", "en")
        - duration (float): Dauer in Sekunden
        - model_size (str): Verwendete Modell-Größe
        - error (str, optional): Fehlermeldung bei Misserfolg
    """
    _whisper_available = _get("WHISPER_AVAILABLE", WHISPER_AVAILABLE)
    _model_size = _get("WHISPER_MODEL_SIZE", WHISPER_MODEL_SIZE)
    _device = _get("WHISPER_DEVICE", WHISPER_DEVICE)
    _compute_type = _get("WHISPER_COMPUTE_TYPE", WHISPER_COMPUTE_TYPE)
    _model_cache = _get("_whisper_model_cache", _whisper_model_cache)
    _whisper_model_cls = _get("WhisperModel", WhisperModel)
    if not _whisper_available:
        log.warning("transcribe_audio_whisper_not_available", file=str(audio_path))
        return {
            "success": False,
            "error": "faster-whisper ist nicht installiert (pip install faster-whisper)",
        }

    log.info("transcribe_audio_start", file=str(audio_path), model_size=_model_size)

    try:
        # Modell aus Cache laden oder frisch initialisieren
        if _model_size not in _model_cache:
            log.info("whisper_model_load", model_size=_model_size)
            _model_cache[_model_size] = _whisper_model_cls(
                _model_size,
                device=_device,
                compute_type=_compute_type,
            )
        model = _model_cache[_model_size]

        segments, info = model.transcribe(str(audio_path), beam_size=5)

        # Segmente zusammenführen
        text_parts: list[str] = []
        duration = 0.0
        for segment in segments:
            text_parts.append(segment.text.strip())
            duration = max(duration, segment.end)

        full_text = " ".join(text_parts)
        detected_language = info.language if hasattr(info, "language") else "unknown"

        log.info(
            "transcribe_audio_done",
            file=str(audio_path),
            language=detected_language,
            duration=duration,
            chars=len(full_text),
        )

        return {
            "success": True,
            "text": full_text,
            "language": detected_language,
            "duration": duration,
            "model_size": _model_size,
        }

    except Exception as exc:
        log.error("transcribe_audio_error", file=str(audio_path), error=str(exc))
        return {
            "success": False,
            "error": f"Transkription fehlgeschlagen: {str(exc)}",
        }
