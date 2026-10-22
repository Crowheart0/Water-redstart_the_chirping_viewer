import importlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import wave

import photo_viewer


class SoundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.sound = self.directory / "鸟鸣.wav"
        with wave.open(str(self.sound), "wb") as output:
            output.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            output.writeframes(b"\0\0" * 800)

    def test_custom_sound_menu_entrypoint_exists(self):
        self.assertTrue(
            hasattr(photo_viewer.ImageViewer, "choose_selection_sound"),
            "The final menu must allow selecting a custom success sound")

    def module(self):
        return importlib.import_module("sound_effects")

    def test_wav_validation_rejects_invalid_files(self):
        module = self.module()
        self.assertEqual(module.validate_wav(str(self.sound)), str(self.sound))
        bad = self.directory / "bad.wav"
        bad.write_bytes(b"not an audio file")
        with self.assertRaises(ValueError):
            module.validate_wav(str(bad))

    def test_sound_preference_preserves_existing_global_settings(self):
        module = self.module()
        config = self.directory / "settings.json"
        config.write_text(json.dumps({"last_dir": "photos", "other": True}))
        module.save_sound_path(config, str(self.sound))
        self.assertEqual(module.load_sound_path(config), str(self.sound))
        self.assertEqual(json.loads(config.read_text())["last_dir"], "photos")
        module.save_sound_path(config, "")
        self.assertEqual(module.load_sound_path(config), "")
        self.assertTrue(json.loads(config.read_text())["other"])

    def test_playback_does_not_block_caller_or_stack_workers(self):
        module = self.module()
        started, release = threading.Event(), threading.Event()
        player = module.SoundPlayer()
        self.addCleanup(player.close)

        def slow_validation(path):
            started.set()
            release.wait(2)
            return path

        with patch.object(module, "validate_wav", side_effect=slow_validation), \
             patch.object(player, "_play_custom") as custom:
            try:
                self.assertTrue(player.play(str(self.sound)))
                self.assertTrue(started.wait(1))
                self.assertFalse(player.play(str(self.sound)))
                custom.assert_not_called()
            finally:
                release.set()
                player._worker.join(2)
        custom.assert_called_once_with(str(self.sound))

    def test_missing_custom_audio_falls_back_to_default(self):
        module = self.module()
        player = module.SoundPlayer()
        self.addCleanup(player.close)
        with patch.object(player, "_play_default") as default:
            player.play(str(self.directory / "missing.wav"))
            player._worker.join(2)
        default.assert_called_once()

    @unittest.skipUnless(os.name == "nt", "Windows sound API")
    def test_native_windows_playback_accepts_valid_pcm_wav(self):
        player = self.module().SoundPlayer()
        self.addCleanup(player.close)
        with patch.object(player, "_play_default") as default:
            player.play(str(self.sound))
            player._worker.join(2)
        self.assertFalse(player._worker.is_alive())
        default.assert_not_called()

    def test_playback_errors_do_not_escape_and_closed_player_stays_closed(self):
        module = self.module()
        player = module.SoundPlayer()
        with patch.object(player, "_play_custom", side_effect=OSError("audio unavailable")), \
             patch.object(player, "_play_default", side_effect=OSError("audio unavailable")):
            player.play(str(self.sound))
            player._worker.join(2)
        player.close()
        self.assertFalse(player.play(str(self.sound)))

    def test_choosing_sound_does_not_rebuild_or_reset_other_menus(self):
        viewer = photo_viewer.ImageViewer.__new__(photo_viewer.ImageViewer)
        viewer.root = Mock()
        viewer.custom_sound_path = ""
        viewer.sound_menu = Mock()
        viewer.create_menu = Mock()
        viewer.get_global_config_path = lambda: self.directory / "global.json"
        with patch("tkinter.filedialog.askopenfilename", return_value=str(self.sound)):
            viewer.choose_selection_sound()
        self.assertEqual(viewer.custom_sound_path, str(self.sound))
        viewer.create_menu.assert_not_called()
        viewer.restore_selection_sound()
        self.assertEqual(viewer.custom_sound_path, "")
        self.assertEqual(self.module().load_sound_path(viewer.get_global_config_path()), "")


if __name__ == "__main__":
    unittest.main()
