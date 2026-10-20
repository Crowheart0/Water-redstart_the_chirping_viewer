import ast
import inspect
import queue
import tempfile
import threading
import time
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import photo_viewer
from image_loader import ImageLoader, read_for_display


class FakeRoot:
    def __init__(self):
        self.jobs = {}
        self.cancelled = []
        self._next_id = 0

    def after(self, delay_ms, callback, *args):
        self._next_id += 1
        job_id = f"after-{self._next_id}"
        self.jobs[job_id] = (delay_ms, lambda: callback(*args))
        return job_id

    def after_cancel(self, job_id):
        self.cancelled.append(job_id)
        self.jobs.pop(job_id, None)

    def run_only_job(self):
        self.assert_job_count(1)
        _, callback = self.jobs.pop(next(iter(self.jobs)))
        callback()

    def assert_job_count(self, expected):
        if len(self.jobs) != expected:
            raise AssertionError(f"expected {expected} jobs, got {len(self.jobs)}")


class MainThreadVariable:
    def __init__(self, value):
        self.value = value
        self.main_thread = threading.current_thread()

    def get(self):
        if threading.current_thread() is not self.main_thread:
            raise AssertionError("Tk-like variable was read outside the main thread")
        return self.value


class PreloadTests(unittest.TestCase):
    def test_background_preload_code_has_no_tk_or_viewer_access(self):
        source = "\n".join(
            textwrap.dedent(inspect.getsource(function))
            for function in (
                ImageLoader._run,
                read_for_display,
            )
        )
        tree = ast.parse(textwrap.dedent(source))
        referenced_names = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        }

        self.assertNotIn("tk", referenced_names)
        self.assertNotIn("root", referenced_names)

    def test_decoder_does_not_construct_tk_variables(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "sample.jpg"
            Image.new("RGB", (320, 200), "red").save(image_path)

            with mock.patch.object(
                photo_viewer.tk,
                "IntVar",
                side_effect=AssertionError("decoder touched Tk"),
            ):
                decoded = photo_viewer.ImageViewer.read_image_fast(str(image_path), 64)

        self.assertIsNotNone(decoded)
        self.assertLessEqual(max(decoded.size), 64)

    def test_cancelled_preload_discards_in_flight_result(self):
        started, release = threading.Event(), threading.Event()
        def decode(_path, _quality):
            started.set()
            release.wait(3)
            return Image.new('RGB', (10, 10))
        loader = ImageLoader(decode)
        self.addCleanup(loader.close)
        loader.request('old.jpg', 2000)
        self.assertTrue(started.wait(3))
        loader.reset()
        release.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with loader.condition:
                if not loader.in_flight:
                    break
            time.sleep(0.005)
        self.assertEqual(loader.cache, {})
        self.assertIsNone(loader.take_result())

    def test_preload_neighbors_reads_settings_on_main_thread(self):
        viewer = photo_viewer.ImageViewer.__new__(photo_viewer.ImageViewer)
        viewer.index = 2
        viewer.images = [f"{idx}.jpg" for idx in range(20)]
        viewer.current_dir = "/photos"
        viewer.image_quality = MainThreadVariable(4000)
        viewer.super_mode = MainThreadVariable(False)
        viewer.low_memory_mode = MainThreadVariable(True)
        neighbors = viewer._preload_neighbors()
        self.assertEqual(len(neighbors), 6)
        self.assertTrue(all(isinstance(name, str) for name in neighbors))
        self.assertTrue(neighbors[0].endswith('3.jpg'))

    def test_preload_window_is_small_and_bounded(self):
        indices = photo_viewer._build_preload_indices(10, 100, photo_viewer.PRELOAD_NORMAL_COUNT)
        self.assertEqual(indices[:2], (9, 8))
        self.assertEqual(len(indices), 2 + photo_viewer.PRELOAD_NORMAL_COUNT)


class ReleaseAssetTests(unittest.TestCase):
    ASSETS = [
        {"name": "Water-redstart_v3.6.0_windows_x64.exe"},
        {"name": "Water-redstart_v3.6.0_macos_arm64.zip"},
        {"name": "Water-redstart_v3.6.0_macos_x64.zip"},
    ]

    def test_windows_selects_exe(self):
        asset = photo_viewer._select_release_asset(self.ASSETS, "win32", "amd64")
        self.assertEqual(asset["name"], "Water-redstart_v3.6.0_windows_x64.exe")

    def test_apple_silicon_selects_arm64_zip(self):
        asset = photo_viewer._select_release_asset(self.ASSETS, "darwin", "arm64")
        self.assertEqual(asset["name"], "Water-redstart_v3.6.0_macos_arm64.zip")

    def test_intel_mac_selects_x64_zip(self):
        asset = photo_viewer._select_release_asset(self.ASSETS, "darwin", "x86_64")
        self.assertEqual(asset["name"], "Water-redstart_v3.6.0_macos_x64.zip")

    def test_mac_does_not_fall_back_to_windows_asset(self):
        asset = photo_viewer._select_release_asset(self.ASSETS[:1], "darwin", "arm64")
        self.assertIsNone(asset)


class SchedulingTests(unittest.TestCase):
    def test_rapid_navigation_is_rendered_once(self):
        viewer = photo_viewer.ImageViewer.__new__(photo_viewer.ImageViewer)
        viewer.root = FakeRoot()
        viewer.images = ["0.jpg", "1.jpg", "2.jpg", "3.jpg"]
        viewer.index = 0
        viewer.history = []
        viewer._navigation_job = None
        viewer._pending_navigation_delta = 0
        viewer._last_navigation_render_at = time.monotonic()
        viewer.load_image = mock.Mock()

        viewer.next_image()
        viewer.next_image()
        viewer.next_image()

        self.assertEqual(viewer.index, 0)
        viewer.root.run_only_job()
        self.assertEqual(viewer.index, 3)
        self.assertEqual([item["index"] for item in viewer.history], [0, 1, 2])
        viewer.load_image.assert_called_once_with()

    def test_first_navigation_renders_immediately(self):
        viewer = photo_viewer.ImageViewer.__new__(photo_viewer.ImageViewer)
        viewer.root = FakeRoot()
        viewer.images = ["0.jpg", "1.jpg"]
        viewer.index = 0
        viewer.history = []
        viewer._navigation_job = None
        viewer._pending_navigation_delta = 0
        viewer._last_navigation_render_at = 0.0
        viewer.load_image = mock.Mock()

        viewer.next_image()

        self.assertEqual(viewer.index, 1)
        viewer.root.assert_job_count(0)
        viewer.load_image.assert_called_once_with()

    def test_config_saves_are_debounced(self):
        viewer = photo_viewer.ImageViewer.__new__(photo_viewer.ImageViewer)
        viewer.root = FakeRoot()
        viewer._config_save_job = None
        viewer.save_config = mock.Mock()

        viewer.schedule_save_config()
        viewer.schedule_save_config()

        viewer.root.run_only_job()
        viewer.save_config.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
