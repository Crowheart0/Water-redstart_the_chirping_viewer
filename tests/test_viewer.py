"""Real Tk integration without touching the user's preferences or photos."""
import os
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from PIL import Image
from photo_viewer import ImageViewer


class ViewerTests(unittest.TestCase):
    def make_root(self):
        try:
            return tk.Tk()
        except tk.TclError as error:
            self.skipTest(f'Tk display unavailable: {error}')

    def wait_image(self, root, viewer):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            root.update()
            if viewer.current_img_obj is not None:
                return
            time.sleep(0.01)
        self.fail('Photo did not appear in Tk')

    def test_zoom_batches_events_and_detail_keeps_view(self):
        with tempfile.TemporaryDirectory() as directory:
            Image.new('RGB', (3200, 2400), 'red').save(os.path.join(directory, 'large.jpg'))
            root = self.make_root()
            root.withdraw()
            with patch('photo_viewer.get_monitor_profile', return_value=None), patch.object(ImageViewer, 'load_config'), patch.object(ImageViewer, 'save_config'), patch.object(ImageViewer, 'get_select_count', return_value=0):
                viewer = ImageViewer(root)
                try:
                    viewer._process_open_folder(directory)
                    self.wait_image(root, viewer)
                    # Let the initial startup callback complete before zooming.
                    end = time.monotonic() + 0.15
                    while time.monotonic() < end:
                        root.update()
                        time.sleep(0.005)
                    self.assertEqual(viewer.current_img_obj.size, (1600, 1200))
                    self.assertGreater(len(viewer.current_img_obj.info['display_levels']), 0)
                    with patch.object(viewer, 'display_image', wraps=viewer.display_image) as rendered:
                        for _ in range(12):
                            viewer.on_mouse_wheel(SimpleNamespace(delta=120, x=100, y=100))
                        # Twelve events enqueue a single frame, no synchronous resizes.
                        rendered.assert_not_called()
                        pending = viewer._render_job
                        self.assertIsNotNone(pending)
                        scale_before, x_before, y_before = viewer.current_scale, viewer.im_x, viewer.im_y
                        end = time.monotonic() + 5
                        while time.monotonic() < end and viewer.current_img_obj.width < 3200:
                            root.update()
                            time.sleep(0.005)
                    self.assertEqual(viewer.current_img_obj.size, (3200, 2400))
                    self.assertAlmostEqual(viewer.current_scale, scale_before / 2)
                    self.assertAlmostEqual(viewer.im_x, x_before * 2)
                    self.assertAlmostEqual(viewer.im_y, y_before * 2)
                    viewer._process_open_folder(directory)
                    self.wait_image(root, viewer)
                    self.assertEqual(viewer.current_img_obj.size, (1600, 1200))
                    self.assertIsNone(viewer._detail_token)
                finally:
                    viewer.close()

    def test_navigation_quality_and_folder_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            first = os.path.join(directory, 'first')
            second = os.path.join(directory, 'second')
            os.mkdir(first)
            os.mkdir(second)
            for index, color in enumerate(('red', 'green', 'blue')):
                Image.new('RGB', (500, 300), color).save(os.path.join(first, f'{index}.png'))
            Image.new('RGB', (300, 500), 'yellow').save(os.path.join(second, 'new.png'))
            root = self.make_root()
            root.withdraw()
            with patch('photo_viewer.get_monitor_profile', return_value=None), patch.object(ImageViewer, 'load_config'), patch.object(ImageViewer, 'save_config'), patch.object(ImageViewer, 'get_select_count', return_value=0):
                viewer = ImageViewer(root)
                try:
                    viewer._process_open_folder(first)
                    self.wait_image(root, viewer)
                    self.assertEqual(viewer.current_img_obj.getpixel((0, 0)), (255, 0, 0))
                    viewer.next_image()
                    viewer.next_image()
                    viewer._cancel_pending_navigation()
                    viewer._pending_navigation_delta = 1
                    viewer._apply_pending_navigation()
                    self.wait_image(root, viewer)
                    self.assertEqual(viewer.current_img_obj.getpixel((0, 0)), (0, 0, 255))
                    viewer.image_quality.set(200)
                    viewer.change_quality()
                    self.wait_image(root, viewer)
                    self.assertEqual(viewer.current_img_obj.size, (200, 120))
                    viewer.prev_image()
                    viewer._process_open_folder(second)
                    self.wait_image(root, viewer)
                    self.assertEqual(viewer.current_img_obj.size, (120, 200))
                    self.assertEqual(viewer.current_img_obj.getpixel((0, 0)), (255, 255, 0))
                    self.assertIsNotNone(viewer.tk_image)
                finally:
                    viewer.close()


if __name__ == '__main__':
    unittest.main()
