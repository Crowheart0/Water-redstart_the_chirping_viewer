import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from photo_viewer import ImageViewer


class FakeCanvas:
    def __init__(self, width=1000, height=800):
        self.width = width
        self.height = height

    def winfo_width(self):
        return self.width

    def winfo_height(self):
        return self.height


class FakeTk:
    def __init__(self, precise_deltas):
        self.precise_deltas = precise_deltas

    def call(self, *_args):
        return self.precise_deltas


class ZoomTests(unittest.TestCase):
    def make_viewer(self, precise_deltas=(0, 10)):
        viewer = ImageViewer.__new__(ImageViewer)
        viewer.current_img_obj = object()
        viewer.is_fit = True
        viewer.current_scale = 1.0
        viewer.im_x = 500.0
        viewer.im_y = 400.0
        viewer.canvas = FakeCanvas()
        viewer.root = SimpleNamespace(tk=FakeTk(precise_deltas))
        viewer.display_calls = 0

        def display_image():
            viewer.display_calls += 1

        viewer.display_image = display_image
        return viewer

    def assert_cursor_anchor_is_stable(self, viewer, event, old_anchor):
        new_anchor = (
            viewer.im_x
            + (event.x - viewer.canvas.winfo_width() / 2) / viewer.current_scale,
            viewer.im_y
            + (event.y - viewer.canvas.winfo_height() / 2) / viewer.current_scale,
        )
        self.assertAlmostEqual(new_anchor[0], old_anchor[0])
        self.assertAlmostEqual(new_anchor[1], old_anchor[1])

    def test_touchpad_scroll_zooms_at_pointer(self):
        viewer = self.make_viewer((0, 10))
        event = SimpleNamespace(x=750, y=200, delta=10)

        result = viewer.on_touchpad_scroll(event)

        self.assertEqual(result, "break")
        self.assertAlmostEqual(viewer.current_scale, 1.1)
        self.assertEqual(viewer.display_calls, 1)
        self.assert_cursor_anchor_is_stable(viewer, event, (750.0, 200.0))

    def test_horizontal_touchpad_scroll_does_not_zoom(self):
        viewer = self.make_viewer((10, 0))
        event = SimpleNamespace(x=750, y=200, delta=655360)

        viewer.on_touchpad_scroll(event)

        self.assertEqual(viewer.current_scale, 1.0)
        self.assertEqual(viewer.display_calls, 0)

    def test_windows_mouse_wheel_uses_120_delta_units(self):
        viewer = self.make_viewer()
        event = SimpleNamespace(x=250, y=600, delta=120)

        with mock.patch.object(sys, "platform", "win32"):
            viewer.on_mouse_wheel(event)

        self.assertAlmostEqual(viewer.current_scale, 1.1)
        self.assert_cursor_anchor_is_stable(viewer, event, (250.0, 600.0))

    def test_touchpad_delta_fallback_decodes_signed_values(self):
        self.assertEqual(ImageViewer._decode_touchpad_delta(10), (0, 10))
        self.assertEqual(ImageViewer._decode_touchpad_delta(65535), (0, -1))
        self.assertEqual(ImageViewer._decode_touchpad_delta(65536), (1, 0))


if __name__ == "__main__":
    unittest.main()
