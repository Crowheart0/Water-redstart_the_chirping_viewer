"""Bounded high-quality preloading, using real workers and tiny pixel buffers."""
import os
import threading
import unittest
from collections import Counter
from unittest.mock import patch

from PIL import Image

from image_loader import ImageLoader


MIB = 1024 * 1024
QUALITY = 8192


class SizedDecoder:
    def __init__(self, frame_bytes=200 * MIB, sizes=None, blocked=()):
        self.frame_bytes = frame_bytes
        self.sizes = sizes or {}
        self.calls = []
        self.condition = threading.Condition()
        self.gates = {name: threading.Event() for name in blocked}

    def __call__(self, path, quality):
        name = os.path.basename(path)
        with self.condition:
            self.calls.append(name)
            self.condition.notify_all()
        gate = self.gates.get(name)
        if gate is not None and not gate.wait(5):
            raise TimeoutError(f'Test did not release {name}')
        image = Image.new('RGB', (2, 2))
        image.info['test_bytes'] = self.sizes.get(name, self.frame_bytes)
        return image

    def started(self, name):
        with self.condition:
            return self.condition.wait_for(lambda: name in self.calls, timeout=3)


class HighQualityCacheTests(unittest.TestCase):
    def setUp(self):
        # Replace size metadata only; exercise the real cache, priority and workers.
        sizing = patch.object(ImageLoader, '_bytes',
                              staticmethod(lambda image: image.info['test_bytes']))
        sizing.start()
        self.addCleanup(sizing.stop)

    def loader(self, decoder, **kwargs):
        loader = ImageLoader(decoder, workers=1, **kwargs)

        def finish():
            loader.close()
            for gate in decoder.gates.values():
                gate.set()
            for thread in loader.threads:
                thread.join(3)
                self.assertFalse(thread.is_alive(), 'Decoder worker did not stop')

        self.addCleanup(finish)
        return loader

    def idle(self, loader):
        with loader.condition:
            self.assertTrue(loader.condition.wait_for(
                lambda: not loader.in_flight and loader.pending is None
                and not loader.prefetch, timeout=3), 'Loader did not become idle')

    @staticmethod
    def key(name):
        return os.path.abspath(name), QUALITY

    @staticmethod
    def names(loader):
        with loader.condition:
            return {os.path.basename(key[0]) for key in loader.cache}

    def assert_bounded(self, loader):
        with loader.condition:
            self.assertEqual(loader.cache_bytes, sum(loader._bytes(image)
                                                    for image in loader.cache.values()))
            self.assertLessEqual(loader.cache_bytes, loader.budget)

    def test_configured_budgets_warm_five_frames_and_shrink_preserves_current(self):
        decoder = SizedDecoder(64 * MIB)
        loader = self.loader(decoder, budget_bytes=320 * MIB,
                             low_memory_budget_bytes=128 * MIB, estimate_prefetch=True)
        neighbors = ['near', 'middle', 'far', 'last']
        loader.request('current', QUALITY, neighbors)
        self.idle(loader)
        self.assertEqual(self.names(loader), {'current', *neighbors})
        self.assertEqual(loader.cache_bytes, 320 * MIB)

        _, hit = loader.request('current', QUALITY, neighbors, low_memory=True)
        self.assertIsNotNone(hit)
        self.idle(loader)
        self.assertEqual(loader.budget, 128 * MIB)
        self.assertEqual(self.names(loader), {'current', 'near'})
        self.assertEqual(Counter(decoder.calls), Counter(['current', *neighbors]))
        self.assert_bounded(loader)

        _, hit = loader.request('current', QUALITY, neighbors)
        self.assertIsNotNone(hit)
        self.idle(loader)
        self.assertEqual(loader.budget, 320 * MIB)
        self.assertEqual(self.names(loader), {'current', *neighbors})
        self.assert_bounded(loader)

    def test_large_rolling_window_decodes_only_current_and_nearest_that_fit(self):
        decoder = SizedDecoder()
        loader = self.loader(decoder, budget_bytes=768 * MIB,
                             low_memory_budget_bytes=192 * MIB, estimate_prefetch=True)
        loader.request('0', QUALITY, [str(index) for index in range(1, 13)])
        self.idle(loader)
        self.assertEqual(decoder.calls, ['0', '1', '2'])
        self.assertEqual(self.names(loader), {'0', '1', '2'})

        for current in range(1, 25):
            with self.subTest(current=current):
                neighbors = [str(current + offset) for offset in range(1, 13)]
                _, hit = loader.request(str(current), QUALITY, neighbors)
                self.assertIsNotNone(hit, f'Rolling frame {current} missed the cache')
                self.idle(loader)
                self.assertEqual(self.names(loader),
                                 {str(current + offset) for offset in range(3)})
                self.assertEqual(len(decoder.calls), current + 3)
                self.assert_bounded(loader)
                # A repeated cache hit must not restart speculation farther away.
                loader.request(str(current), QUALITY, neighbors)
                self.idle(loader)
                self.assertEqual(len(decoder.calls), current + 3)
        self.assertEqual(Counter(decoder.calls), Counter(map(str, range(27))))

    def test_foreground_over_budget_is_delivered_and_stops_speculation(self):
        decoder = SizedDecoder(200 * MIB)
        loader = self.loader(decoder, budget_bytes=128 * MIB, estimate_prefetch=True)
        token, hit = loader.request('current', QUALITY, ['near', 'far'])
        self.assertIsNone(hit)
        self.idle(loader)
        result_token, image, error = loader.take_result()
        self.assertEqual(result_token, token)
        self.assertIsNotNone(image)
        self.assertIsNone(error)
        self.assertEqual(decoder.calls, ['current'])
        self.assertEqual(self.names(loader), set())
        self.assert_bounded(loader)

    def test_actual_decoded_bytes_control_cache_admission(self):
        decoder = SizedDecoder(64 * MIB, sizes={'too-large': 300 * MIB})
        loader = self.loader(decoder, budget_bytes=256 * MIB, estimate_prefetch=True)
        loader.request('current', QUALITY, ['too-large', 'near'])
        self.idle(loader)
        self.assertEqual(decoder.calls, ['current', 'too-large', 'near'])
        self.assertEqual(self.names(loader), {'current', 'near'})
        self.assertEqual(loader.cache_bytes, 128 * MIB)
        self.assertEqual(loader.known_sizes[self.key('too-large')], 300 * MIB)
        self.assert_bounded(loader)

    def test_exact_known_sizes_override_current_estimate_for_mixed_frames(self):
        decoder = SizedDecoder(200 * MIB, sizes={'large': 400 * MIB, 'small': 50 * MIB})
        loader = self.loader(decoder, budget_bytes=650 * MIB,
                             low_memory_budget_bytes=200 * MIB, estimate_prefetch=True)
        loader.request('small', QUALITY, ['large', 'current'])
        self.idle(loader)
        self.assertEqual(self.names(loader), {'small', 'large', 'current'})

        loader.request('current', QUALITY, ['large', 'small'], low_memory=True)
        self.idle(loader)
        self.assertEqual(self.names(loader), {'current'})
        with loader.condition:
            self.assertEqual(loader._estimated_bytes(self.key('large')), 400 * MIB)
            self.assertEqual(loader._estimated_bytes(self.key('small')), 50 * MIB)
            self.assertEqual(loader._estimated_bytes(self.key('unknown')), 200 * MIB)

        loader.request('current', QUALITY, ['large', 'small', 'unknown'])
        self.idle(loader)
        self.assertEqual(self.names(loader), {'current', 'large', 'small'})
        self.assertNotIn('unknown', decoder.calls)
        self.assertEqual(loader.cache_bytes, 650 * MIB)
        self.assert_bounded(loader)

    def test_unknown_current_uses_largest_known_frame_until_decode_finishes(self):
        decoder = SizedDecoder(200 * MIB, sizes={'large': 400 * MIB, 'small': 50 * MIB},
                               blocked=['new-current'])
        loader = self.loader(decoder, budget_bytes=768 * MIB, estimate_prefetch=True)
        with loader.condition:
            self.assertEqual(loader._estimated_bytes(self.key('unknown')), 1)
        loader.request('small', QUALITY, ['large', 'current'])
        self.idle(loader)
        loader.request('new-current', QUALITY, ['large', 'small', 'current'])
        self.assertTrue(decoder.started('new-current'))
        with loader.condition:
            self.assertEqual(loader._estimated_bytes(self.key('unknown')), 400 * MIB)
        decoder.gates['new-current'].set()
        self.idle(loader)
        with loader.condition:
            self.assertEqual(loader._estimated_bytes(self.key('unknown')), 200 * MIB)
        self.assert_bounded(loader)

    def test_default_budgets_and_disabled_estimation_preserve_existing_behavior(self):
        decoder = SizedDecoder(100 * MIB)
        loader = self.loader(decoder)
        self.assertEqual(loader.budget, 256 * MIB)
        loader.request('current', QUALITY, ['near', 'middle', 'far', 'too-far'])
        self.idle(loader)
        self.assertEqual(decoder.calls, ['current', 'near', 'middle', 'far', 'too-far'])
        with loader.condition:
            self.assertEqual(loader._estimated_bytes(self.key('unknown')), 1)
            self.assertEqual(loader._estimated_bytes(self.key('near')), 100 * MIB)
        loader.request('current', QUALITY, low_memory=True)
        self.idle(loader)
        self.assertEqual(loader.budget, 96 * MIB)
        loader.request('current', QUALITY)
        self.idle(loader)
        self.assertEqual(loader.budget, 256 * MIB)

    def test_budgets_must_be_positive_integers(self):
        for option in ('budget_bytes', 'low_memory_budget_bytes'):
            for invalid in (0, -1, 1.5, '128', None, True, False):
                with self.subTest(option=option, invalid=invalid):
                    with self.assertRaises(ValueError):
                        self.loader(SizedDecoder(), **{option: invalid})


if __name__ == '__main__':
    unittest.main()
