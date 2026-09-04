"""Speak to search, recognised on this machine.

Chromium's Web Speech API sends audio to Google, and needs a key Merlin does
not have, so this does not use it. Recognition runs locally with Vosk: audio
never leaves the machine and it works with no network once the model is there.

Neither the library nor the model ships with Merlin. Vosk is a pip install and
the small English model is about 40 MB, so both are fetched on first use, after
saying what is about to happen. Nothing is downloaded silently.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.request
import zipfile

from PyQt6.QtCore import QObject, pyqtSignal

from . import settings as cfg

MODEL_DIR = os.path.join(cfg.DATA_DIR, "speech")
MODEL_NAME = "vosk-model-small-en-us-0.15"
MODEL_URL = f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip"
SAMPLE_RATE = 16000
SILENCE_LEVEL = 500          # mean absolute sample value counted as silence
SILENCE_CHUNKS = 12          # consecutive quiet reads before stopping
MAX_SECONDS = 15


def model_path(settings=None) -> str:
    """Where the model lives: a folder you chose, or the one Merlin fetches.

    The settings object is passed in rather than constructed here, so a change
    made in the dialog takes effect immediately instead of only after it has
    been written to disk and read back.
    """
    chosen = ""
    if settings is not None:
        chosen = (settings.get("speech_model_path") or "").strip()
    if chosen and os.path.isdir(chosen):
        return chosen
    return os.path.join(MODEL_DIR, MODEL_NAME)


def model_ready(settings=None) -> bool:
    folder = model_path(settings)
    return os.path.isdir(folder) and os.path.isdir(os.path.join(folder, "am"))


def library_ready() -> bool:
    try:
        import vosk  # noqa: F401

        return True
    except Exception:                                    # noqa: BLE001
        return False


def audio_ready() -> bool:
    try:
        from PyQt6.QtMultimedia import QAudioSource  # noqa: F401

        return True
    except Exception:                                    # noqa: BLE001
        return False


def what_is_missing(settings=None) -> list[str]:
    missing = []
    if not audio_ready():
        missing.append("PyQt6 multimedia support, for reading the microphone")
    if not library_ready():
        missing.append("the vosk package, which does the recognition")
    if not model_ready(settings):
        missing.append(f"the {MODEL_NAME} model, about 40 MB")
    return missing


class Setup(QObject):
    """Installs the library and downloads the model, off the UI thread."""

    progress = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def __init__(self, settings=None, parent=None):
        super().__init__(parent)
        self.settings = settings

    def run(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        if not library_ready():
            self.progress.emit("Installing vosk...")
            import subprocess
            import sys

            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "vosk"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    check=True, timeout=600)
            except Exception as exc:                     # noqa: BLE001
                self.finished.emit(False, f"Could not install vosk: {exc}")
                return

        if not model_ready(self.settings):
            self.progress.emit(f"Downloading {MODEL_NAME}, about 40 MB...")
            os.makedirs(MODEL_DIR, exist_ok=True)
            archive = os.path.join(MODEL_DIR, MODEL_NAME + ".zip")
            try:
                # urllib's default User-Agent is refused by some servers with a
                # 403, which looks like the model having moved when it has not
                request = urllib.request.Request(
                    MODEL_URL, headers={"User-Agent": "Merlin Browser"})
                with urllib.request.urlopen(request, timeout=180) as response, \
                        open(archive, "wb") as handle:
                    handle.write(response.read())
                self.progress.emit("Unpacking the model...")
                with zipfile.ZipFile(archive) as bundle:
                    bundle.extractall(MODEL_DIR)
            except Exception as exc:                     # noqa: BLE001
                self.finished.emit(False, (
                    f"Could not fetch the model: {exc}\n\n"
                    f"You can download it yourself from\n{MODEL_URL}\n"
                    "unzip it anywhere, and point Settings, Search at the "
                    "folder."))
                return
            finally:
                try:
                    os.remove(archive)
                except OSError:
                    pass

        if not model_ready(self.settings):
            self.finished.emit(False, "The model did not unpack as expected.")
            return
        self.finished.emit(True, "Speech recognition is ready.")


class Dictation(QObject):
    """Record from the microphone until quiet, then transcribe."""

    started = pyqtSignal()
    level = pyqtSignal(float)          # 0..1, for showing that it is hearing
    finished = pyqtSignal(str)         # the text, or empty
    failed = pyqtSignal(str)

    def __init__(self, settings=None, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._source = None
        self._device = None
        self._chunks: list[bytes] = []
        self._quiet = 0
        self._timer = None

    def listening(self) -> bool:
        return self._source is not None

    def start(self) -> None:
        missing = what_is_missing(self.settings)
        if missing:
            self.failed.emit("Not set up yet: " + "; ".join(missing))
            return
        try:
            from PyQt6.QtCore import QTimer
            from PyQt6.QtMultimedia import (QAudioFormat, QAudioSource,
                                            QMediaDevices)

            fmt = QAudioFormat()
            fmt.setSampleRate(SAMPLE_RATE)
            fmt.setChannelCount(1)
            fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)

            device = QMediaDevices.defaultAudioInput()
            if device is None or device.isNull():
                self.failed.emit("No microphone was found.")
                return

            self._source = QAudioSource(device, fmt, self)
            self._device = self._source.start()
            if self._device is None:
                self._source = None
                self.failed.emit("The microphone could not be opened.")
                return

            self._chunks = []
            self._quiet = 0
            self._elapsed = 0
            self._timer = QTimer(self)
            self._timer.setInterval(100)
            self._timer.timeout.connect(self._poll)
            self._timer.start()
            self.started.emit()
        except Exception as exc:                         # noqa: BLE001
            self._source = None
            self.failed.emit(f"Could not start listening: {exc}")

    def _poll(self) -> None:
        if self._device is None:
            return
        data = bytes(self._device.readAll())
        if data:
            self._chunks.append(data)
            loudness = _mean_level(data)
            self.level.emit(min(1.0, loudness / 3000.0))
            self._quiet = self._quiet + 1 if loudness < SILENCE_LEVEL else 0
        else:
            self._quiet += 1

        self._elapsed += 1
        # stop on a stretch of quiet, or when the clip gets too long
        if self._quiet >= SILENCE_CHUNKS or self._elapsed >= MAX_SECONDS * 10:
            self.stop()

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        if self._source is not None:
            self._source.stop()
            self._source = None
        self._device = None

        audio = b"".join(self._chunks)
        self._chunks = []
        if len(audio) < SAMPLE_RATE:          # under half a second of samples
            self.finished.emit("")
            return
        threading.Thread(target=self._transcribe, args=(audio,),
                         daemon=True).start()

    def _transcribe(self, audio: bytes) -> None:
        try:
            import vosk

            vosk.SetLogLevel(-1)
            model = vosk.Model(model_path(self.settings))
            recogniser = vosk.KaldiRecognizer(model, SAMPLE_RATE)
            recogniser.AcceptWaveform(audio)
            result = json.loads(recogniser.FinalResult())
            self.finished.emit((result.get("text") or "").strip())
        except Exception as exc:                         # noqa: BLE001
            self.failed.emit(f"Could not transcribe: {exc}")


def _mean_level(data: bytes) -> float:
    """Average absolute amplitude of 16-bit little-endian samples."""
    if len(data) < 2:
        return 0.0
    total = 0
    count = len(data) // 2
    for i in range(0, count * 2, 2):
        value = data[i] | (data[i + 1] << 8)
        if value >= 32768:
            value -= 65536
        total += value if value >= 0 else -value
    return total / count
