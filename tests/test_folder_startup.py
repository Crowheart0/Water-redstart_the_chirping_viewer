"""Nested native folder dialogs must not race the initial-folder timer."""
import unittest
from unittest.mock import Mock, patch

from photo_viewer import ImageViewer


class FolderStartupTests(unittest.TestCase):
    def make_viewer(self):
        viewer = ImageViewer.__new__(ImageViewer)
        viewer.root = Mock()
        viewer._initial_folder_pending = True
        viewer._copy_pending = False
        viewer.destination_dialog = None
        viewer.images = ["old.jpg"]
        viewer.load_image = Mock()
        viewer.prompt_destination = Mock(
            side_effect=lambda: setattr(viewer, "destination_dialog", Mock()))
        viewer.loading_splash = Mock()
        viewer._process_open_folder = Mock()
        return viewer

    def test_source_chooser_pauses_initial_load_before_nested_event_loop(self):
        viewer = self.make_viewer()

        def choose(**kwargs):
            # Windows' native folder picker runs Tk's pending callbacks.
            viewer._load_initial_folder()
            return "new-photo-folder"

        with patch("tkinter.filedialog.askdirectory", side_effect=choose):
            viewer.open_folder()
        viewer.prompt_destination.assert_not_called()
        viewer.root.after.call_args.args[1]()
        viewer._process_open_folder.assert_called_once_with("new-photo-folder")
        self.assertIsNone(viewer.destination_dialog)

    def test_cancel_source_chooser_restores_initial_folder_loading(self):
        viewer = self.make_viewer()

        def cancel(**kwargs):
            viewer._load_initial_folder()
            return ""

        with patch("tkinter.filedialog.askdirectory", side_effect=cancel):
            viewer.open_folder()
        viewer.prompt_destination.assert_not_called()
        self.assertTrue(viewer._initial_folder_pending)
        viewer._load_initial_folder()
        viewer.prompt_destination.assert_called_once()
        viewer._process_open_folder.assert_not_called()

    def test_initial_decode_error_is_deferred_until_destination_confirmed(self):
        viewer = self.make_viewer()
        viewer.images = ["broken.jpg", "valid.jpg"]
        viewer.index = 0
        viewer._load_token = "first"
        viewer.image_loader = Mock()
        viewer.image_loader.take_result.side_effect = [
            ("first", None, ValueError("broken image")), None]
        viewer.detail_loader = Mock()
        viewer.detail_loader.take_result.return_value = None
        viewer.destination_dialog = Mock()
        viewer._poll_image_loader()
        self.assertEqual(viewer.index, 0)
        viewer.load_image.assert_not_called()
        viewer.destination_dialog = None
        viewer._poll_image_loader()
        self.assertEqual(viewer.index, 1, "deferred decode failure must not be lost")
        viewer.load_image.assert_called_once()


if __name__ == "__main__":
    unittest.main()
