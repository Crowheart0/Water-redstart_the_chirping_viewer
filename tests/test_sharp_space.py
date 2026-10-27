"""Real Tk coverage for selected-quality first frames and no-I/O Space zoom."""
import gc
import os
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from PIL import Image
import photo_viewer
from photo_viewer import ImageViewer


class SharpSpaceTests(unittest.TestCase):
    def setUp(self):
        gc.collect()  # Reclaim prior Tk objects on the UI thread, not a decoder.
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = self.temp.name
        for index, color in enumerate(('red', 'green', 'blue')):
            Image.new('RGB', (3200, 2000), color).save(
                os.path.join(self.directory, f'{index}.png'))
        self.calls = []
        self.started, self.release = threading.Event(), threading.Event()
        decoder = photo_viewer.read_for_display

        def controlled(path, quality, **kwargs):
            name = os.path.basename(path)
            self.calls.append((name, quality))
            if name == '1.png':
                self.started.set()
                if not self.release.wait(5):
                    raise TimeoutError('Test failed to release the slow frame')
            return decoder(path, quality, **kwargs)

        self.enterContext(patch('photo_viewer.read_for_display', side_effect=controlled))
        self.enterContext(patch('photo_viewer.get_monitor_profile', return_value=None))
        for name in ('load_config', 'save_config', 'prompt_destination', 'switch_to_english_ime'):
            self.enterContext(patch.object(ImageViewer, name))
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f'Tk display unavailable: {error}')
        self.root.withdraw()
        self.viewer = ImageViewer(self.root)
        self.addCleanup(self.clean_viewer)
        self.viewer._process_open_folder(self.directory)
        self.wait_for(lambda: self.viewer.current_img_obj is not None
                      and not self.viewer._image_pending)

    def clean_viewer(self):
        self.release.set()
        for job in self.root.tk.call('after', 'info'):
            self.root.after_cancel(job)
        v = self.viewer
        loaders = [v.image_loader]
        v.close()
        for loader in loaders:
            for thread in loader.threads:
                thread.join(3)
                self.assertFalse(thread.is_alive())
        self.viewer = self.root = None
        del v
        gc.collect()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            self.root.update()
            if predicate():
                return
            time.sleep(0.002)
        self.fail('Timed out waiting for selected-quality image')

    def test_first_display_already_has_selected_quality(self):
        self.assertEqual(self.viewer.current_img_obj.size, (3200, 2000))
        self.assertIn(('0.png', 8000), self.calls)
        self.assertNotIn(('0.png', 1600), self.calls)

    def test_first_space_render_reuses_ready_full_image_without_decoding(self):
        v = self.viewer
        self.release.set()
        self.wait_for(lambda: not v.image_loader.in_flight and not v.image_loader.prefetch)
        before = v.current_img_obj
        calls_before = list(self.calls)
        with patch('photo_viewer.read_for_display', side_effect=AssertionError('Space started decoding')):
            v.on_space_press()
            v.display_image()
            self.assertTrue(v.is_magnifying)
            self.assertEqual(v.current_img_obj.size, (3200, 2000))
            self.assertIs(v.current_img_obj, before)
            for _ in range(5):
                self.root.update()
                time.sleep(0.01)
            self.assertEqual(self.calls, calls_before)
            self.assertIs(v.current_img_obj, before)
            self.assertNotIn('高清细节加载中', self.root.title())

    def test_space_repeat_and_release_restore_manual_view(self):
        v = self.viewer
        v.is_fit = False
        v.current_scale, v.im_x, v.im_y = 0.6, 1200.0, 800.0
        before = v.current_img_obj
        v.on_space_press()
        v.display_image()
        saved = dict(v.pre_magnify_state)
        v.on_space_press()
        self.assertEqual(v.pre_magnify_state, saved)
        v.on_space_release()
        v.display_image()
        self.assertFalse(v.is_magnifying)
        self.assertFalse(v.is_fit)
        self.assertEqual((v.current_scale, v.im_x, v.im_y), (0.6, 1200.0, 800.0))
        self.assertIs(v.current_img_obj, before)

    def test_delayed_next_frame_never_displays_a_lower_quality_placeholder(self):
        v = self.viewer
        self.assertTrue(self.started.wait(2))
        before, item = v.current_img_obj, v._canvas_image_id
        v.index = 1
        v.load_image()
        self.assertTrue(v._image_pending)
        self.assertIs(v.current_img_obj, before)
        self.assertIn('当前: 0.png', self.root.title())
        self.release.set()
        self.wait_for(lambda: not v._image_pending)
        self.assertEqual(v.current_img_obj.size, (3200, 2000))
        self.assertEqual(v.current_img_obj.getpixel((0, 0)), (0, 128, 0))
        self.assertEqual(v._canvas_image_id, item)
        self.assertIn('当前: 1.png', self.root.title())

    def test_ready_neighbor_swaps_selected_quality_synchronously(self):
        v = self.viewer
        key = (os.path.abspath(os.path.join(self.directory, '2.png')), 8000)
        # Wait until prefetch is idle, then require a selected-quality cache hit.
        self.release.set()
        self.wait_for(lambda: not v.image_loader.in_flight and not v.image_loader.prefetch)
        self.assertIn(key, v.image_loader.cache)
        with patch('photo_viewer.read_for_display', side_effect=AssertionError('Cache hit decoded')):
            v.index = 2
            v.load_image()
            self.assertFalse(v._image_pending)
            self.assertEqual(v.current_img_obj.size, (3200, 2000))
            self.assertEqual(v.current_img_obj.getpixel((0, 0)), (0, 0, 255))

    def test_quality_change_rejects_old_inflight_quality(self):
        v = self.viewer
        self.assertTrue(self.started.wait(2))
        v.index = 1
        v.load_image()
        v.image_quality.set(2000)
        v.change_quality()
        self.release.set()
        self.wait_for(lambda: not v._image_pending)
        self.wait_for(lambda: not v.image_loader.in_flight)
        v._poll_image_loader()
        self.assertEqual(v.current_img_obj.size, (2000, 1250))
        self.assertTrue(all(key[1] == 2000 for key in v.image_loader.cache))

    def test_folder_change_rejects_old_inflight_selected_quality(self):
        v = self.viewer
        v.index = 1
        v.load_image()
        other = os.path.join(self.directory, 'other')
        os.mkdir(other)
        Image.new('RGB', (2400, 1600), 'yellow').save(os.path.join(other, 'new.png'))
        v._process_open_folder(other)
        self.release.set()
        self.wait_for(lambda: v.current_img_obj is not None and not v._image_pending)
        self.wait_for(lambda: not v.image_loader.in_flight)
        v._poll_image_loader()
        self.assertEqual(v.current_img_obj.size, (2400, 1600))
        self.assertEqual(v.current_img_obj.getpixel((0, 0)), (255, 255, 0))
        self.assertIn('当前: new.png', self.root.title())


if __name__ == '__main__':
    unittest.main()
