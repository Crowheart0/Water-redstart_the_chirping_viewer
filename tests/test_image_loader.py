import io
import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageCms
import rawpy

from image_loader import ImageLoader, prepare_image, read_image


class DecodeTests(unittest.TestCase):
    def test_jpeg_orientation_resize_and_detached_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'portrait.jpg')
            exif = Image.Exif()
            exif[274] = 6
            Image.new('RGB', (400, 200), 'red').save(path, exif=exif)
            image = read_image(path, 100)
            os.remove(path)
            self.assertEqual(image.size, (50, 100))
            self.assertGreater(image.getpixel((20, 20))[0], 240)

    def test_icc_transform_changes_pixels(self):
        image = Image.new('LAB', (10, 10), (140, 160, 170))
        image.info['icc_profile'] = ImageCms.ImageCmsProfile(ImageCms.createProfile('LAB')).tobytes()
        expected = ImageCms.profileToProfile(image, ImageCms.createProfile('LAB'),
                                            ImageCms.createProfile('sRGB'), outputMode='RGB')
        actual = prepare_image(image, 100)
        self.assertEqual(actual.getpixel((0, 0)), expected.getpixel((0, 0)))

    def test_broken_icc_and_transparency(self):
        image = Image.new('RGBA', (10, 10), (30, 60, 90, 120))
        image.info['icc_profile'] = b'broken profile'
        actual = prepare_image(image, 100)
        self.assertEqual(actual.getpixel((0, 0)), (30, 60, 90, 120))

    def test_tagged_adobe_rgb_preview(self):
        image = Image.new('RGB', (10, 10), (120, 160, 80))
        image.getexif()[40961] = 2
        actual = prepare_image(image, 100)
        # A wider-gamut green needs less red/blue after conversion to sRGB.
        red, green, blue = actual.getpixel((0, 0))
        self.assertLess(red, 120)
        self.assertLess(blue, 80)
        self.assertTrue(155 <= green <= 165)

    def fake_raw(self, thumb=None, error=None):
        raw = unittest.mock.MagicMock()
        raw.__enter__.return_value = raw
        raw.sizes = SimpleNamespace(flip=6, width=400, height=200)
        if error is not None:
            raw.extract_thumb.side_effect = error
        else:
            raw.extract_thumb.return_value = thumb
        raw.postprocess.return_value = np.zeros((200, 400, 3), dtype=np.uint8)
        return raw

    def test_raw_jpeg_preview_does_not_unpack_sensor(self):
        data = io.BytesIO()
        Image.new('RGB', (40, 20), 'green').save(data, format='JPEG')
        raw = self.fake_raw(SimpleNamespace(format=rawpy.ThumbFormat.JPEG, data=data.getvalue()))
        with patch('image_loader.rawpy.RawPy', return_value=raw):
            actual = read_image('nikon.nef', 100)
        raw.open_file.assert_called_once_with('nikon.nef')
        raw.unpack.assert_not_called()
        raw.postprocess.assert_not_called()
        self.assertEqual(actual.size, (20, 40))

    def test_raw_bitmap_is_array_and_rotates(self):
        pixels = np.full((20, 40, 3), (20, 70, 150), dtype=np.uint8)
        raw = self.fake_raw(SimpleNamespace(format=rawpy.ThumbFormat.BITMAP, data=pixels))
        with patch('image_loader.rawpy.RawPy', return_value=raw):
            actual = read_image('nikon.NEF', 100)
        self.assertEqual(actual.size, (20, 40))
        self.assertEqual(actual.getpixel((0, 0)), (20, 70, 150))
        raw.unpack.assert_not_called()

    def test_missing_or_unsupported_preview_uses_camera_wb(self):
        for error in (rawpy.LibRawNoThumbnailError(), rawpy.LibRawUnsupportedThumbnailError(), OSError('bad jpeg')):
            with self.subTest(error=type(error).__name__):
                raw = self.fake_raw(error=error)
                with patch('image_loader.rawpy.RawPy', return_value=raw):
                    read_image('nikon.nef', 100)
                raw.unpack.assert_called_once()
                kwargs = raw.postprocess.call_args.kwargs
                self.assertTrue(kwargs['use_camera_wb'])
                self.assertFalse(kwargs['use_auto_wb'])
                self.assertTrue(kwargs['no_auto_bright'])
                self.assertEqual(kwargs['output_color'], rawpy.ColorSpace.sRGB)


class WorkerTests(unittest.TestCase):
    def wait_result(self, loader):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = loader.take_result()
            if result is not None:
                return result
            time.sleep(0.005)
        self.fail('Timed out waiting for decoder')

    def test_rapid_requests_discard_old_and_prioritize_current(self):
        started, release = threading.Event(), threading.Event()
        calls = []
        def decoder(path, quality):
            name = os.path.basename(path)
            calls.append(name)
            if name == 'old':
                started.set()
                self.assertTrue(release.wait(3))
            return Image.new('RGB', (10, 10))
        loader = ImageLoader(decoder)
        self.addCleanup(loader.close)
        loader.request('old', 100, ['far'])
        self.assertTrue(started.wait(3))
        loader.request('skipped', 100)
        token, _ = loader.request('new', 100)
        release.set()
        self.assertEqual(self.wait_result(loader)[0], token)
        self.assertEqual(calls, ['old', 'new'])

    def test_folder_reset_rejects_inflight_cache(self):
        started, release = threading.Event(), threading.Event()
        def decoder(path, quality):
            if os.path.basename(path) == 'old.nef':
                started.set()
                self.assertTrue(release.wait(3))
            return Image.new('RGB', (10, 10))
        loader = ImageLoader(decoder)
        self.addCleanup(loader.close)
        loader.request('old.nef', 100)
        self.assertTrue(started.wait(3))
        loader.reset()
        token, _ = loader.request('other-folder/new.nef', 200)
        release.set()
        self.assertEqual(self.wait_result(loader)[0], token)
        self.assertNotIn((os.path.abspath('old.nef'), 100), loader.cache)

    def test_cache_bounded_and_quality_is_part_of_key(self):
        loader = ImageLoader(lambda path, quality: Image.new('RGB', (64, 64)))
        self.addCleanup(loader.close)
        loader.request('one', 100)
        self.wait_result(loader)
        _, hit = loader.request('one', 100)
        self.assertIsNotNone(hit)
        _, miss = loader.request('one', 200)
        self.assertIsNone(miss)
        self.wait_result(loader)
        with loader.condition:
            loader.budget = 64 * 64 * 4
            loader._trim()
            self.assertLessEqual(loader.cache_bytes, loader.budget)
            self.assertEqual(len(loader.cache), 1)

    def test_prefetch_promoted_without_duplicate_decode(self):
        started, release = threading.Event(), threading.Event()
        calls = []
        def decoder(path, quality):
            name = os.path.basename(path)
            calls.append(name)
            if name == 'next':
                started.set()
                self.assertTrue(release.wait(3))
            return Image.new('RGB', (10, 10))
        loader = ImageLoader(decoder)
        self.addCleanup(loader.close)
        loader.request('first', 100, ['next'])
        self.wait_result(loader)
        self.assertTrue(started.wait(3))
        token, _ = loader.request('next', 100)
        release.set()
        self.assertEqual(self.wait_result(loader)[0], token)
        self.assertEqual(calls.count('next'), 1)

    def test_error_returned_and_worker_survives(self):
        def decoder(path, quality):
            if os.path.basename(path) == 'broken':
                raise ValueError('bad file')
            return Image.new('RGB', (10, 10))
        loader = ImageLoader(decoder)
        self.addCleanup(loader.close)
        loader.request('broken', 100)
        self.assertEqual(self.wait_result(loader)[2], 'bad file')
        loader.request('good', 100)
        self.assertIsNotNone(self.wait_result(loader)[1])

    def test_parallel_prefetch_does_not_decode_requested_frame_twice(self):
        started, release = threading.Event(), threading.Event()
        calls = []
        def decoder(path, quality):
            name = os.path.basename(path)
            calls.append(name)
            if name == 'next':
                started.set()
                self.assertTrue(release.wait(3))
            return Image.new('RGB', (10, 10))
        loader = ImageLoader(decoder, workers=3)
        self.addCleanup(loader.close)
        loader.request('first', 100, ['next', 'next', 'third'])
        self.wait_result(loader)
        self.assertTrue(started.wait(3))
        token, _ = loader.request('next', 100)
        release.set()
        self.assertEqual(self.wait_result(loader)[0], token)
        self.assertEqual(calls.count('next'), 1)


if __name__ == '__main__':
    unittest.main()
