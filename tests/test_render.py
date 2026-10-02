import os
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from image_render import build_levels, render_viewport
from image_loader import read_for_display, ImageLoader
from windows_decoder import decode_windows, WICError


class RenderTests(unittest.TestCase):
    def test_reduced_level_used_for_screen_view(self):
        image = build_levels(Image.new('RGB', (8000, 6000), 'red'))
        self.assertEqual([factor for factor, _ in image.info['display_levels']], [2, 4, 8, 16])
        level = image.info['display_levels'][2][1]
        with patch.object(level, 'resize', wraps=level.resize) as resized:
            result = render_viewport(image, (0, 0, 8000, 6000), (1000, 750))
        resized.assert_called_once()
        self.assertEqual(result.getpixel((0, 0)), (255, 0, 0))
        self.assertGreater(ImageLoader._bytes(image), 8000 * 6000 * 4)

    def test_zoom_uses_full_resolution_crop(self):
        image = build_levels(Image.new('RGB', (4000, 3000), 'blue'))
        with patch.object(image, 'resize', wraps=image.resize) as resized:
            result = render_viewport(image, (1200, 800, 1700, 1200), (1000, 800))
        resized.assert_called_once()
        self.assertEqual(result.size, (1000, 800))

    def test_windows_unavailable_fallback_is_visible(self):
        with patch('windows_decoder.decode_windows', side_effect=WICError('no codec')), patch('image_loader.read_image', return_value=Image.new('RGB', (100, 100))):
            image = read_for_display('nikon.nef', 1600)
        self.assertIn('回退', image.info['decoder_label'])

    def test_windows_preview_cache_is_lossless_and_invalidated_by_file_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'sample.nef')
            with open(path, 'wb') as stream:
                stream.write(b'raw placeholder')
            decoded = Image.new('RGB', (100, 80), (40, 70, 130))
            from unittest.mock import MagicMock
            raw = MagicMock()
            raw.__enter__.return_value = raw
            raw.sizes.flip = 0
            with patch('image_loader.tempfile.gettempdir', return_value=directory), patch('windows_decoder.decode_windows', return_value=decoded) as decode, patch('image_loader.rawpy.RawPy', return_value=raw):
                first = read_for_display(path, 1600)
                second = read_for_display(path, 1600)
                self.assertEqual(decode.call_count, 1)
                self.assertEqual(first.getpixel((0, 0)), second.getpixel((0, 0)))
                self.assertIn('缓存', second.info['decoder_label'])
                with open(path, 'ab') as stream:
                    stream.write(b'changed')
                read_for_display(path, 1600)
                self.assertEqual(decode.call_count, 2)

    @unittest.skipUnless(os.name == 'nt', 'Windows only')
    def test_actual_windows_decoder_pixels_and_scaling(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, '颜色测试.png')
            Image.new('RGB', (400, 200), (40, 80, 120)).save(path)
            result = decode_windows(path, 100)
            self.assertEqual(result.size, (100, 50))
            self.assertEqual(result.getpixel((20, 20)), (40, 80, 120))
            os.remove(path)


if __name__ == '__main__':
    unittest.main()
