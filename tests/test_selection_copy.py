"""Selection must not be acknowledged until every file is safely copied."""
import errno
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import photo_viewer


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "照片"
        self.target = Path(self.temp.name) / "已选"
        self.source.mkdir()
        self.target.mkdir()
        (self.source / "鸟.jpg").write_bytes(b"jpeg" * 1024)
        (self.source / "鸟.nef").write_bytes(b"raw" * 2048)
        self.viewer = photo_viewer.ImageViewer.__new__(photo_viewer.ImageViewer)
        v = self.viewer
        v.current_dir = str(self.source)
        v.select_folder_name = str(self.target)
        v.images = ["鸟.jpg", "next.jpg"]
        v.image_groups = {"鸟.jpg": ["鸟.jpg", "鸟.nef"]}
        v.index = 0
        v.history = []
        v._copy_pending = False
        v._copy_results = queue.SimpleQueue()
        v._low_space_warned = set()
        v.custom_sound_path = ""
        v.sound_player = Mock()
        v.save_raw = Mock(get=Mock(return_value=True))
        v.save_jpeg = Mock(get=Mock(return_value=True))
        v._navigation_job = None
        v._pending_navigation_delta = 0
        v.root = Mock()
        v.top_info_label = Mock()
        v.progress_scale = Mock()
        v.update_title = Mock()
        v.load_image = Mock()
        v.image_loader = Mock()
        v.detail_loader = Mock()
        v.save_config = Mock()
        self.dialog = self.enterContext(patch.object(photo_viewer, "messagebox"))
        if hasattr(photo_viewer, "winsound"):
            self.enterContext(patch.object(photo_viewer.winsound, "Beep"))

    def drain(self):
        """Run scheduled main-thread callbacks until the copy worker finishes."""
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            callbacks = self.viewer.root.after.call_args_list[:]
            self.viewer.root.after.reset_mock()
            for call in callbacks:
                call.args[1](*call.args[2:])
            if not self.viewer._copy_pending:
                return
            time.sleep(0.005)
        self.fail("copy result never reached the UI")

    def test_whole_raw_group_space_checked_before_copy(self):
        with patch("shutil.disk_usage", return_value=SimpleNamespace(free=5000)):
            self.viewer.copy_and_next()
            self.drain()
        self.assertEqual(self.viewer.index, 0, "disk-full must not advance")
        self.assertEqual(self.viewer.history, [])
        self.viewer.sound_player.play.assert_not_called()
        self.assertEqual(list(self.target.iterdir()), [])
        self.dialog.showwarning.assert_called_once()
        text = self.dialog.showwarning.call_args.args[1]
        self.assertIn(str(self.target), text)
        self.assertIn("空间不足", text)

    def test_success_commits_group_and_undo_after_completion(self):
        self.viewer.copy_and_next()
        self.drain()
        self.assertEqual(self.viewer.index, 1)
        self.assertEqual(len(self.viewer.history), 1)
        self.viewer.sound_player.play.assert_called_once_with("")
        for name in ("鸟.jpg", "鸟.nef"):
            self.assertEqual((self.target / name).read_bytes(),
                             (self.source / name).read_bytes())
        self.viewer.undo_action()
        self.assertEqual(self.viewer.index, 0)
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertEqual(len(list(self.source.iterdir())), 2)

    def test_mid_copy_disk_full_rolls_back_and_warns_on_main_thread(self):
        main_thread = threading.get_ident()
        import shutil
        original = shutil.copyfileobj
        calls = []

        def fail_second(src, dst, *args, **kwargs):
            calls.append(threading.get_ident())
            if len(calls) == 2:
                dst.write(b"partial")
                raise OSError(errno.ENOSPC, "No space left on device")
            return original(src, dst, *args, **kwargs)

        self.dialog.showwarning.side_effect = (
            lambda *a, **k: self.assertEqual(threading.get_ident(), main_thread))
        with patch("shutil.copyfileobj", side_effect=fail_second):
            self.viewer.copy_and_next()
            self.drain()
        self.assertEqual(self.viewer.history, [])
        self.assertEqual(self.viewer.index, 0)
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertTrue(all(ident != main_thread for ident in calls))
        self.dialog.showwarning.assert_called_once()
        self.assertIn("空间不足", str(self.dialog.showwarning.call_args))

    def test_no_acknowledgement_until_background_write_finishes(self):
        import shutil
        original = shutil.copyfileobj
        started, release = threading.Event(), threading.Event()

        def slow_copy(*args, **kwargs):
            started.set()
            if not release.wait(3):
                raise TimeoutError("test copy was not released")
            return original(*args, **kwargs)

        with patch("shutil.copyfileobj", side_effect=slow_copy):
            try:
                self.viewer.copy_and_next()
                self.assertTrue(started.wait(2))
                self.assertTrue(self.viewer._copy_pending)
                self.assertEqual(self.viewer.index, 0)
                self.assertEqual(self.viewer.history, [])
                self.viewer.load_image.assert_not_called()
                self.viewer.copy_and_next()
                self.viewer.next_image()
                self.viewer.close()
                self.viewer.root.destroy.assert_not_called()
                self.assertEqual(self.viewer.index, 0)
            finally:
                release.set()
                self.drain()
        self.assertEqual(self.viewer.index, 1)
        self.assertEqual(len(self.viewer.history), 1)

    def test_existing_different_file_is_preserved(self):
        existing = self.target / "鸟.nef"
        existing.write_bytes(b"earlier selection")
        self.viewer.copy_and_next()
        self.drain()
        self.assertEqual(existing.read_bytes(), b"earlier selection")
        self.assertFalse((self.target / "鸟.jpg").exists())
        self.assertEqual(self.viewer.index, 0)
        self.assertEqual(self.viewer.history, [])
        self.dialog.showwarning.assert_called_once()

    def test_existing_identical_file_is_not_deleted_by_undo(self):
        existing = self.target / "鸟.jpg"
        existing.write_bytes((self.source / "鸟.jpg").read_bytes())
        self.viewer.copy_and_next()
        self.drain()
        self.viewer.undo_action()
        self.assertEqual(existing.read_bytes(), (self.source / "鸟.jpg").read_bytes())
        self.assertFalse((self.target / "鸟.nef").exists())

    def test_busy_copy_blocks_navigation_close_and_repeat_selection(self):
        self.viewer._copy_pending = True
        self.viewer.copy_and_next()
        self.viewer.next_image()
        self.viewer.prev_image()
        self.viewer.undo_action()
        self.viewer.on_progress_change("1")
        self.viewer._process_open_folder(str(self.target))
        self.viewer.show_end_dialog()
        self.viewer.close()
        self.assertEqual(self.viewer.index, 0)
        self.assertEqual(self.viewer.history, [])
        self.viewer.root.destroy.assert_not_called()
        self.viewer.load_image.assert_not_called()

    def test_low_remaining_space_warns_once_and_still_saves(self):
        with patch("shutil.disk_usage", return_value=SimpleNamespace(free=100_000)):
            self.viewer.copy_and_next()
            self.drain()
            self.viewer.index = 0
            self.viewer.copy_and_next()
            self.drain()
        self.assertEqual(self.viewer.index, 1)
        self.dialog.showwarning.assert_called_once()
        self.assertIn("空间即将不足", str(self.dialog.showwarning.call_args))

    def test_unavailable_destination_warns_without_advancing(self):
        with patch("shutil.disk_usage", side_effect=PermissionError("Access denied")):
            self.viewer.copy_and_next()
            self.drain()
        self.assertEqual(self.viewer.index, 0)
        self.assertEqual(list(self.target.iterdir()), [])
        self.dialog.showwarning.assert_called_once()

    def test_raw_only_selection_checks_only_raw_size(self):
        self.viewer.save_jpeg.get.return_value = False
        with patch("shutil.disk_usage", return_value=SimpleNamespace(free=7000)):
            self.viewer.copy_and_next()
            self.drain()
        self.assertEqual(self.viewer.index, 1)
        self.assertEqual([p.name for p in self.target.iterdir()], ["鸟.nef"])

    def test_jpeg_only_selection_and_undo_preserve_originals(self):
        self.viewer.save_raw.get.return_value = False
        self.viewer.copy_and_next()
        self.drain()
        self.assertEqual([p.name for p in self.target.iterdir()], ["鸟.jpg"])
        self.viewer.undo_action()
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertEqual(len(list(self.source.iterdir())), 2)

    def test_no_format_selected_does_not_acknowledge_or_advance(self):
        self.viewer.save_raw.get.return_value = False
        self.viewer.save_jpeg.get.return_value = False
        self.viewer.copy_and_next()
        self.drain()
        self.assertEqual(self.viewer.index, 0)
        self.assertEqual(self.viewer.history, [])
        self.assertEqual(list(self.target.iterdir()), [])
        self.viewer.sound_player.play.assert_not_called()
        self.dialog.showwarning.assert_called_once()
        self.assertIn("至少", str(self.dialog.showwarning.call_args))

    def test_single_format_group_ignores_hidden_paired_preferences(self):
        self.viewer.save_raw.get.return_value = False
        self.viewer.save_jpeg.get.return_value = False
        self.viewer.image_groups = {"鸟.jpg": ["鸟.jpg"]}
        self.viewer.copy_and_next()
        self.drain()
        self.assertEqual(self.viewer.index, 1)
        self.assertTrue((self.target / "鸟.jpg").exists())

    def test_mixed_group_recognizes_uppercase_raw_and_jpeg(self):
        self.assertTrue(photo_viewer.ImageViewer._is_raw_jpeg_group(["bird.NEF", "bird.JPEG"]))
        self.assertFalse(photo_viewer.ImageViewer._is_raw_jpeg_group(["bird.png", "bird.JPG"]))

    def prepare_late_decode_error(self):
        self.viewer._load_token = "slow-decoder"
        self.viewer.image_loader.take_result.return_value = (
            "slow-decoder", None, ValueError("Unsupported RAW"))
        self.viewer.detail_loader.take_result.return_value = None
        self.viewer.show_end_dialog = Mock()

    def test_decode_failure_during_disk_warning_cannot_change_frame(self):
        self.prepare_late_decode_error()
        self.dialog.showwarning.side_effect = lambda *a, **k: self.viewer._poll_image_loader()
        with patch("shutil.disk_usage", return_value=SimpleNamespace(free=0)):
            self.viewer.copy_and_next()
            self.drain()
        self.assertEqual(self.viewer.index, 0)
        self.viewer.load_image.assert_not_called()
        self.viewer.show_end_dialog.assert_not_called()

    def test_decode_failure_after_disk_warning_still_keeps_failed_selection(self):
        self.prepare_late_decode_error()
        with patch("shutil.disk_usage", return_value=SimpleNamespace(free=0)):
            self.viewer.copy_and_next()
            self.drain()
        self.viewer._poll_image_loader()
        self.assertEqual(self.viewer.index, 0)
        self.viewer.load_image.assert_not_called()

    def test_decode_failure_during_low_space_warning_does_not_skip_extra_frame(self):
        self.prepare_late_decode_error()
        self.dialog.showwarning.side_effect = lambda *a, **k: self.viewer._poll_image_loader()
        with patch("shutil.disk_usage", return_value=SimpleNamespace(free=100_000)):
            self.viewer.copy_and_next()
            self.drain()
        self.assertEqual(self.viewer.index, 1)
        self.viewer.load_image.assert_called_once()
        self.viewer.show_end_dialog.assert_not_called()


if __name__ == "__main__":
    unittest.main()
