"""Optional selection-success audio; playback never calls Tk or blocks the UI."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import wave

if sys.platform == "win32":
    import winsound


def validate_wav(path):
    path = os.path.abspath(path)
    if Path(path).suffix.lower() != ".wav":
        raise ValueError("请选择 WAV 音频文件（建议使用简短的 PCM 提示音）。")
    try:
        with wave.open(path, "rb") as audio:
            if audio.getnframes() <= 0 or audio.getcomptype() != "NONE":
                raise ValueError("WAV 文件为空或不是受支持的 PCM 音频。")
    except (wave.Error, EOFError) as error:
        raise ValueError("无法读取该 WAV 文件，请使用有效的 PCM WAV 音频。") from error
    return path


def load_sound_path(config_path):
    try:
        with open(config_path, encoding="utf-8") as stream:
            data = json.load(stream)
        path = data.get("selection_sound", "") if isinstance(data, dict) else ""
        return path if isinstance(path, str) else ""
    except (OSError, ValueError):
        return ""


def save_sound_path(config_path, path):
    config_path = Path(config_path)
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data["selection_sound"] = path
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=config_path.parent,
            prefix=".birdviewer-sound-", suffix=".tmp", delete=False,
        ) as stream:
            temporary = stream.name
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, config_path)
        temporary = None
    finally:
        if temporary is not None:
            os.remove(temporary)


class SoundPlayer:
    def __init__(self):
        self._busy = threading.Lock()
        self._state = threading.Lock()
        self._closed = False
        self._process = None
        self._worker = None

    def play(self, path=""):
        if self._closed or not self._busy.acquire(blocking=False):
            return False
        try:
            self._worker = threading.Thread(
                target=self._run, args=(path,), daemon=True, name="selection-sound")
            self._worker.start()
        except Exception:
            self._busy.release()
            return False
        return True

    def _run(self, path):
        try:
            if path:
                try:
                    self._play_custom(validate_wav(path))
                    return
                except Exception:
                    pass
            self._play_default()
        except Exception:
            # Audio/device errors must never fail a successful selection.
            pass
        finally:
            self._busy.release()

    def _stop_process(self):
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
        self._process = None

    def _play_custom(self, path):
        with self._state:
            if self._closed:
                return
            if sys.platform == "win32":
                winsound.PlaySound(
                    path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
            else:
                command = "/usr/bin/afplay" if sys.platform == "darwin" else shutil.which("aplay")
                if not command:
                    raise OSError("No WAV player available")
                self._stop_process()
                self._process = subprocess.Popen(
                    [command, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _play_default(self):
        with self._state:
            if self._closed:
                return
            if sys.platform == "win32":
                winsound.PlaySound(None, 0)
            elif sys.platform == "darwin":
                self._stop_process()
                self._process = subprocess.Popen(
                    ["/usr/bin/osascript", "-e", "beep"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
        if sys.platform == "win32":
            for pitch, duration in ((3000, 30), (3800, 30), (4500, 50)):
                if self._closed:
                    break
                winsound.Beep(pitch, duration)

    def close(self):
        with self._state:
            self._closed = True
            try:
                self._stop_process()
                if sys.platform == "win32":
                    winsound.PlaySound(None, 0)
            except Exception:
                pass
