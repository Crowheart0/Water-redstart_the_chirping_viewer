"""Real Tk regressions for retained-frame navigation and action identity."""
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


class SmoothNavigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = self.temp.name
        for index, color in enumerate(('red', 'green', 'blue')):
            Image.new('RGB', (160, 100), color).save(os.path.join(self.directory, f'{index}.png'))
        self.started, self.release = threading.Event(), threading.Event()
        self.fail_slow = False
        original_decoder = photo_viewer.read_for_display

        def delayed(path, quality, **kwargs):
            if os.path.basename(path) == '1.png':
                self.started.set()
                if not self.release.wait(5):
                    raise TimeoutError('test did not release decoder')
                if self.fail_slow:
                    raise ValueError('intentionally broken preview')
            return original_decoder(path, quality, **kwargs)

        self.enterContext(patch('photo_viewer.read_for_display', side_effect=delayed))
        self.enterContext(patch('photo_viewer.get_monitor_profile', return_value=None))
        for method in ('load_config', 'save_config', 'prompt_destination', 'switch_to_english_ime'):
            self.enterContext(patch.object(ImageViewer, method))
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f'Tk display unavailable: {error}')
        self.root.withdraw()
        self.viewer = ImageViewer(self.root)
        self.addCleanup(self.clean_viewer)
        self.viewer._process_open_folder(self.directory)
        self.wait_for(lambda: self.viewer.current_img_obj is not None)
        self.assertTrue(self.started.wait(2), 'neighbor was not preloaded')

    def clean_viewer(self):
        self.release.set()
        self.viewer._copy_pending = False
        for job in self.root.tk.call('after', 'info'):
            self.root.after_cancel(job)
        self.viewer.close()
        for worker in self.viewer.image_loader.threads:
            worker.join(2)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.root.update()
            if predicate():
                return
            time.sleep(0.002)
        self.fail('Timed out waiting for Tk/decoder state')

    def request_slow_frame(self):
        self.viewer.index = 1
        self.viewer.load_image()

    def test_cache_miss_keeps_bitmap_canvas_and_displayed_filename(self):
        v = self.viewer
        previous_image, previous_tk = v.current_img_obj, v.tk_image
        previous_item = v._canvas_image_id
        self.request_slow_frame()
        self.assertIs(v.current_img_obj, previous_image)
        self.assertIs(v.tk_image, previous_tk)
        self.assertEqual(v.canvas.type(previous_item), 'image')
        self.assertEqual(v.canvas.find_all(), (previous_item,))
        self.assertIn('当前: 0.png', self.root.title())
        # Other UI callbacks must not relabel the retained bitmap as 1.png.
        v.update_title()
        self.assertIn('当前: 0.png', self.root.title())
        self.release.set()
        self.wait_for(lambda: v.current_img_obj is not previous_image)
        self.assertEqual(v.current_img_obj.getpixel((0, 0)), (0, 128, 0))
        self.assertEqual(v._canvas_image_id, previous_item)
        self.assertIn('当前: 1.png', self.root.title())

    def test_pending_frame_cannot_be_selected(self):
        self.request_slow_frame()
        with patch('photo_viewer.copy_selection', return_value=([], 1024**3)):
            self.viewer.copy_and_next()
            self.assertFalse(self.viewer._copy_pending)
            self.assertEqual(self.viewer.history, [])

    def test_pending_frame_cannot_copy_or_describe_wrong_file(self):
        self.request_slow_frame()
        with patch('photo_viewer.subprocess.run') as clipboard, \
             patch.object(self.viewer, '_show_single_properties') as properties:
            self.viewer.copy_to_os_clipboard()
            self.viewer.show_image_properties()
        clipboard.assert_not_called()
        properties.assert_not_called()

    def test_pending_frame_space_does_not_decode_another_file(self):
        self.request_slow_frame()
        retained = self.viewer.current_img_obj
        with patch.object(self.viewer.image_loader, 'request') as request:
            self.viewer.on_space_press()
            self.viewer.display_image()
        request.assert_not_called()
        self.assertIs(self.viewer.current_img_obj, retained)

    def test_ready_preview_swaps_without_poll_or_decode(self):
        v = self.viewer
        key = (os.path.abspath(os.path.join(self.directory, '2.png')), 8000)
        self.wait_for(lambda: key in v.image_loader.cache)
        previous_item = v._canvas_image_id
        with patch('photo_viewer.read_for_display', side_effect=AssertionError('cache hit decoded')):
            v.index = 2
            v.load_image()
            self.assertEqual(v.current_img_obj.getpixel((0, 0)), (0, 0, 255))
            self.assertEqual(v._canvas_image_id, previous_item)

    def test_obsolete_slow_result_cannot_replace_latest_cached_frame(self):
        v = self.viewer
        key = (os.path.abspath(os.path.join(self.directory, '2.png')), 8000)
        self.wait_for(lambda: key in v.image_loader.cache)
        self.request_slow_frame()
        v.index = 2
        v.load_image()
        self.release.set()
        self.wait_for(lambda: not v.image_loader.in_flight)
        v._poll_image_loader()
        self.assertEqual(v.current_img_obj.getpixel((0, 0)), (0, 0, 255))
        self.assertIn('当前: 2.png', self.root.title())

    def test_pending_old_folder_cannot_leak_into_new_folder(self):
        v = self.viewer
        self.request_slow_frame()
        second = os.path.join(self.directory, 'other')
        os.mkdir(second)
        Image.new('RGB', (80, 160), 'yellow').save(os.path.join(second, 'new.png'))
        v._process_open_folder(second)
        self.release.set()
        self.wait_for(lambda: v.current_img_obj is not None and not getattr(v, '_image_pending', False))
        self.assertEqual(v.current_img_obj.getpixel((0, 0)), (255, 255, 0))
        self.assertIn('当前: new.png', self.root.title())

    def test_failed_preview_skips_without_erasing_retained_frame(self):
        v = self.viewer
        previous_item = v._canvas_image_id
        self.fail_slow = True
        self.request_slow_frame()
        self.assertEqual(v.canvas.type(previous_item), 'image')
        self.release.set()
        self.wait_for(lambda: v.index == 2 and not getattr(v, '_image_pending', False))
        self.assertEqual(v._canvas_image_id, previous_item)
        self.assertEqual(v.current_img_obj.getpixel((0, 0)), (0, 0, 255))

    def test_actual_navigation_changes_preload_direction(self):
        v = self.viewer
        v.index = 2
        v.load_image()
        self.assertEqual(v._navigation_direction, 1)
        v.index = 1
        v.load_image()
        self.assertEqual(v._navigation_direction, -1)
        self.assertTrue(v._preload_neighbors()[0].endswith('0.png'))

    def test_selection_resumes_for_the_newly_displayed_file(self):
        v = self.viewer
        self.request_slow_frame()
        self.release.set()
        self.wait_for(lambda: not getattr(v, '_image_pending', False))
        with patch.object(v.sound_player, 'play'):
            v.copy_and_next()
            self.wait_for(lambda: not v._copy_pending)
        destination = os.path.join(self.directory, 'SELECT')
        self.assertEqual(os.listdir(destination), ['1.png'])
        with Image.open(os.path.join(destination, '1.png')) as selected:
            self.assertEqual(selected.getpixel((0, 0)), (0, 128, 0))

    def test_reversing_order_starts_a_forward_window_in_the_new_order(self):
        v = self.viewer
        v.index = 2
        v.load_image()
        v.reverse_var.set(True)
        v.toggle_reverse()
        self.assertEqual(v.index, 0)
        self.assertEqual(v._navigation_direction, 1)

    def test_fast_jump_past_end_does_not_relabel_an_older_pending_result(self):
        v = self.viewer
        self.request_slow_frame()
        with patch.object(v, 'show_end_dialog') as completed:
            v.index = len(v.images)
            v.load_image()
            self.release.set()
            self.wait_for(lambda: not getattr(v, '_image_pending', False))
            self.assertEqual(v.current_img_obj.getpixel((0, 0)), (0, 0, 255))
            self.assertIn('当前: 2.png', self.root.title())
            completed.assert_called_once()



if __name__ == '__main__':
    unittest.main()
