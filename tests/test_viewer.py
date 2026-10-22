"""Real Tk integration without touching the user's preferences or photos."""
import os
import json
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from PIL import Image
from photo_viewer import ImageViewer


class ViewerTests(unittest.TestCase):
    def setUp(self):
        # Existing rendering tests do not interact with the destination dialog.
        self.destination_prompt = self.enterContext(
            patch.object(ImageViewer, 'prompt_destination', create=True))

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

    def test_full_destination_can_retry_last_photo_before_completion_dialog(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, 'bird.jpg')
            Image.new('RGB', (120, 80), 'green').save(source)
            root = self.make_root()
            root.withdraw()
            with patch('photo_viewer.get_monitor_profile', return_value=None), \
                 patch.object(ImageViewer, 'load_config'), \
                 patch.object(ImageViewer, 'save_config'), \
                 patch.object(ImageViewer, 'show_end_dialog') as completed, \
                 patch('photo_viewer.messagebox.showwarning') as warning:
                viewer = ImageViewer(root)
                try:
                    self.assertEqual(viewer.menubar.entrycget("end", "label"), "音效")
                    viewer._process_open_folder(directory)
                    self.wait_image(root, viewer)
                    with patch('shutil.disk_usage', return_value=SimpleNamespace(free=0)):
                        viewer.copy_and_next()
                        deadline = time.monotonic() + 3
                        while viewer._copy_pending and time.monotonic() < deadline:
                            root.update()
                            time.sleep(0.005)
                    self.assertFalse(viewer._copy_pending)
                    self.assertEqual(viewer.history, [])
                    completed.assert_not_called()
                    warning.assert_called_once()
                    viewer.copy_and_next()
                    deadline = time.monotonic() + 3
                    while viewer._copy_pending and time.monotonic() < deadline:
                        root.update()
                        time.sleep(0.005)
                    self.assertFalse(viewer._copy_pending)
                    self.assertTrue(os.path.exists(os.path.join(directory, 'SELECT', 'bird.jpg')))
                    self.assertEqual(len(viewer.history), 1)
                    completed.assert_called_once()
                finally:
                    for job in root.tk.call('after', 'info'):
                        root.after_cancel(job)
                    viewer.close()

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

    def test_paired_checkboxes_keep_state_between_frames_and_config_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ('first', 'second'):
                Image.new('RGB', (100, 80), 'green').save(os.path.join(directory, name + '.JPG'))
                with open(os.path.join(directory, name + '.NEF'), 'wb') as stream:
                    stream.write(b'raw placeholder; paired JPEG is used for display')
            root = self.make_root()
            root.withdraw()
            with patch('photo_viewer.get_monitor_profile', return_value=None), \
                 patch.object(ImageViewer, 'get_global_config_path',
                              return_value=os.path.join(directory, 'global.json')):
                viewer = ImageViewer(root)
                try:
                    viewer._process_open_folder(directory)
                    self.wait_image(root, viewer)
                    self.assertTrue(hasattr(viewer, 'save_raw'), 'paired saving controls are missing')
                    self.assertEqual(viewer.format_frame.winfo_manager(), 'pack')
                    viewer.save_jpeg.set(False)
                    viewer.index = 1
                    viewer.load_image()
                    self.wait_image(root, viewer)
                    self.assertTrue(viewer.save_raw.get())
                    self.assertFalse(viewer.save_jpeg.get())
                    viewer.save_config()
                    with open(viewer.config_file, encoding='utf-8') as stream:
                        config = json.load(stream)
                    self.assertEqual((config['save_raw'], config['save_jpeg']), (True, False))
                    viewer.save_jpeg.set(True)
                    viewer.load_config()
                    self.assertFalse(viewer.save_jpeg.get())
                    viewer.image_groups[viewer.images[viewer.index]] = [viewer.images[viewer.index]]
                    viewer.update_title()
                    self.assertEqual(viewer.format_frame.winfo_manager(), '')
                finally:
                    for job in root.tk.call('after', 'info'):
                        root.after_cancel(job)
                    viewer.close()


class DestinationDialogTests(ViewerTests):
    # Only run the destination-specific methods here, not inherited test cases.
    test_full_destination_can_retry_last_photo_before_completion_dialog = None
    test_zoom_batches_events_and_detail_keeps_view = None
    test_navigation_quality_and_folder_switch = None
    test_paired_checkboxes_keep_state_between_frames_and_config_reload = None

    def setUp(self):
        pass

    def exercise_destination(self, choose_other=False, startup=False):
        with tempfile.TemporaryDirectory() as directory:
            Image.new('RGB', (100, 80), 'blue').save(os.path.join(directory, 'bird.jpg'))
            root = self.make_root()
            root.withdraw()
            with patch('photo_viewer.get_monitor_profile', return_value=None), \
                 patch.object(ImageViewer, 'load_config'), \
                 patch.object(ImageViewer, 'save_config'), \
                 patch('os.getcwd', return_value=directory if startup else os.getcwd()):
                viewer = ImageViewer(root)
                try:
                    if not startup:
                        viewer._process_open_folder(directory)
                    self.wait_image(root, viewer)
                    self.assertTrue(hasattr(viewer, 'destination_dialog'),
                                    'opening a photo folder must ask for its destination')
                    dialog = viewer.destination_dialog
                    self.assertIsNotNone(dialog)
                    self.assertTrue(dialog.winfo_exists())
                    self.assertEqual(viewer.select_folder_name, 'SELECT')
                    buttons = [child for child in dialog.winfo_children()
                               if isinstance(child, tk.Button)]
                    if choose_other:
                        target = os.path.join(directory, 'custom')
                        os.mkdir(target)
                        with patch('tkinter.filedialog.askdirectory', return_value=target) as browse:
                            next(button for button in buttons
                                 if button.cget('text') == '选择其他文件夹…').invoke()
                        self.assertEqual(browse.call_args.kwargs['initialdir'], directory)
                        self.assertEqual(viewer.select_folder_name, target)
                    else:
                        next(button for button in buttons
                             if button.cget('text') == '使用 SELECT').invoke()
                        self.assertEqual(viewer.select_folder_name, 'SELECT')
                    self.assertIsNone(viewer.destination_dialog)
                    viewer.load_image()
                    root.update()
                    self.assertIsNone(viewer.destination_dialog, 'frame changes must not reprompt')
                finally:
                    if getattr(viewer, 'destination_dialog', None):
                        viewer.destination_dialog.destroy()
                        viewer.destination_dialog = None
                    for job in root.tk.call('after', 'info'):
                        root.after_cancel(job)
                    viewer.close()

    def test_new_folder_prompts_with_default_select(self):
        self.exercise_destination()

    def test_browse_starts_at_source_and_accepts_custom_target(self):
        self.exercise_destination(choose_other=True)

    def test_startup_with_photos_also_prompts(self):
        self.exercise_destination(startup=True)


if __name__ == '__main__':
    unittest.main()
