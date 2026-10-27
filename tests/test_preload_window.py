"""Deterministic coverage for bounded, rolling multi-photo preloading."""
import os
import threading
import unittest
from collections import Counter
from unittest.mock import patch

from PIL import Image

from image_loader import ImageLoader


MIB = 1024 * 1024


class ControlledDecoder:
    def __init__(self, frame_bytes=64 * MIB, blocked=()):
        self.frame_bytes = frame_bytes
        self.condition = threading.Condition()
        self.calls = []
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
        image.info['test_bytes'] = self.frame_bytes
        return image

    def started(self, name):
        with self.condition:
            return self.condition.wait_for(lambda: name in self.calls, timeout=3)

    def release(self, name):
        self.gates[name].set()


class PreloadWindowTests(unittest.TestCase):
    def setUp(self):
        # Exercise the actual cache and workers without allocating 256 MiB.
        sizing = patch.object(ImageLoader, '_bytes',
                              staticmethod(lambda image: image.info['test_bytes']))
        sizing.start()
        self.addCleanup(sizing.stop)

    def loader(self, decoder, workers=3):
        loader = ImageLoader(decoder, workers=workers)

        def finish():
            loader.close()
            for gate in decoder.gates.values():
                gate.set()
            for thread in loader.threads:
                thread.join(3)
                self.assertFalse(thread.is_alive(), 'Decoder worker did not stop')

        self.addCleanup(finish)
        return loader

    def wait_for(self, loader, predicate, message):
        with loader.condition:
            self.assertTrue(loader.condition.wait_for(predicate, timeout=3), message)

    def idle(self, loader):
        self.wait_for(loader, lambda: not loader.in_flight and loader.pending is None
                      and not loader.prefetch, 'Loader did not become idle')

    @staticmethod
    def names(loader):
        with loader.condition:
            return {os.path.basename(key[0]) for key in loader.cache}

    def cached(self, loader, names):
        self.wait_for(loader, lambda: set(names) <= self.names(loader),
                      f'Frames were not cached: {names}')

    def assert_bounded(self, loader):
        with loader.condition:
            self.assertEqual(loader.cache_bytes, sum(loader._bytes(image)
                                                    for image in loader.cache.values()))
            self.assertLessEqual(loader.cache_bytes, loader.budget)

    def test_full_cache_hit_rolls_the_window_and_decodes_new_neighbor(self):
        decoder = ControlledDecoder()
        loader = self.loader(decoder, workers=1)
        loader.request('0', 100, ['1', '2', '3'])
        self.idle(loader)
        self.assertEqual(loader.cache_bytes, loader.budget)

        _, hit = loader.request('1', 100, ['2', '3', '4'])
        self.assertIsNotNone(hit)
        self.assertTrue(decoder.started('4'), 'Full cache stopped rolling prefetch')
        self.idle(loader)
        self.assertEqual(self.names(loader), {'1', '2', '3', '4'})
        self.assert_bounded(loader)

    def test_window_shift_discards_obsolete_cache_before_it_is_full(self):
        decoder = ControlledDecoder()
        loader = self.loader(decoder)
        loader.request('old', 100, ['current'])
        self.idle(loader)

        _, hit = loader.request('current', 100, ['next'])
        self.assertIsNotNone(hit)
        self.idle(loader)
        self.assertEqual(self.names(loader), {'current', 'next'})

    def test_late_nearest_neighbor_replaces_far_frame_and_preserves_current(self):
        decoder = ControlledDecoder(128 * MIB, blocked=['near'])
        loader = self.loader(decoder)
        loader.request('current', 100, ['near', 'far'])
        self.assertTrue(decoder.started('near'))
        self.cached(loader, ['current', 'far'])

        decoder.release('near')
        self.idle(loader)
        self.assertEqual(self.names(loader), {'current', 'near'})
        self.assert_bounded(loader)

    def test_late_old_foreground_cannot_evict_current_or_nearer_neighbor(self):
        decoder = ControlledDecoder(128 * MIB, blocked=['old'])
        loader = self.loader(decoder)
        loader.request('old', 100, ['current', 'near'])
        self.assertTrue(decoder.started('old'))
        self.cached(loader, ['current', 'near'])

        _, hit = loader.request('current', 100, ['near', 'old'])
        self.assertIsNotNone(hit)
        decoder.release('old')
        self.idle(loader)
        self.assertEqual(self.names(loader), {'current', 'near'})
        self.assertIsNone(loader.take_result())
        self.assert_bounded(loader)

    def test_obsolete_inflight_completion_is_discarded_without_reset(self):
        decoder = ControlledDecoder(blocked=['old'])
        loader = self.loader(decoder, workers=1)
        loader.request('old', 100, ['skipped'])
        self.assertTrue(decoder.started('old'))
        token, _ = loader.request('current', 100, ['near'])

        decoder.release('old')
        self.idle(loader)
        self.assertEqual(self.names(loader), {'current', 'near'})
        self.assertEqual(decoder.calls, ['old', 'current', 'near'])
        self.assertEqual(loader.take_result()[0], token)

    def test_long_rolling_sequence_stays_hits_and_decodes_each_frame_once(self):
        decoder = ControlledDecoder()
        loader = self.loader(decoder)
        loader.request('0', 100, ['1', '2', '3'])
        self.idle(loader)

        for current in range(1, 25):
            with self.subTest(current=current):
                neighbors = [str(current + offset) for offset in (1, 2, 3)]
                _, hit = loader.request(str(current), 100, neighbors)
                self.assertIsNotNone(hit, f'Rolling frame {current} missed the cache')
                self.idle(loader)
                self.assertEqual(self.names(loader), {str(current), *neighbors})
                self.assert_bounded(loader)
        self.assertEqual(Counter(decoder.calls), Counter(map(str, range(28))))

    def test_unfittable_far_frame_is_not_redecoded_on_repeated_hit_requests(self):
        decoder = ControlledDecoder(100 * MIB, blocked=['current', 'near', 'far'])
        loader = self.loader(decoder)
        loader.request('current', 100, ['near', 'far'])
        for name in ('current', 'near', 'far'):
            self.assertTrue(decoder.started(name))
        decoder.release('current')
        self.cached(loader, ['current'])
        decoder.release('near')
        self.cached(loader, ['current', 'near'])
        decoder.release('far')
        self.idle(loader)

        for _ in range(5):
            _, hit = loader.request('current', 100, ['near', 'far'])
            self.assertIsNotNone(hit)
            self.idle(loader)
        self.assertEqual(Counter(decoder.calls), Counter(current=1, near=1, far=1))
        self.assertEqual(self.names(loader), {'current', 'near'})
        self.assert_bounded(loader)

    def test_budget_shrink_protects_requested_cached_frame(self):
        decoder = ControlledDecoder()
        loader = self.loader(decoder, workers=1)
        loader.request('current', 100, ['near', 'middle', 'far'])
        self.idle(loader)

        _, hit = loader.request('current', 100, ['near', 'middle', 'far'], low_memory=True)
        self.assertIsNotNone(hit, 'Budget shrink evicted the requested cache hit')
        self.idle(loader)
        self.assertEqual(self.names(loader), {'current'})
        self.assertEqual(decoder.calls.count('current'), 1)
        self.assert_bounded(loader)

    def test_latest_foreground_precedes_queued_speculation_and_is_deduplicated(self):
        decoder = ControlledDecoder(blocked=['old', 'near', 'middle', 'current'])
        loader = self.loader(decoder)
        loader.request('old', 100, ['near', 'middle', 'far'])
        for name in ('old', 'near', 'middle'):
            self.assertTrue(decoder.started(name))
        loader.request('skipped', 100)
        loader.request('current', 100, ['near', 'middle', 'far'])
        decoder.release('near')
        self.assertTrue(decoder.started('current'))
        self.assertEqual(decoder.calls[-1], 'current')
        token, _ = loader.request('current', 100, ['near', 'middle', 'far'])
        for gate in decoder.gates.values():
            gate.set()
        self.idle(loader)
        self.assertEqual(loader.take_result()[0], token)
        self.assertEqual(decoder.calls.count('current'), 1)
        self.assertNotIn('skipped', decoder.calls)
        self.assertNotIn('old', self.names(loader))

    def test_reset_and_close_reject_late_results(self):
        for action in ('reset', 'close'):
            with self.subTest(action=action):
                decoder = ControlledDecoder(blocked=['old'])
                loader = self.loader(decoder)
                loader.request('old', 100)
                self.assertTrue(decoder.started('old'))
                getattr(loader, action)()
                decoder.release('old')
                self.idle(loader)
                self.assertEqual(self.names(loader), set())
                self.assertIsNone(loader.take_result())

    def test_return_to_inflight_foreground_reuses_decode_with_latest_token(self):
        decoder = ControlledDecoder(blocked=['first'])
        loader = self.loader(decoder, workers=1)
        loader.request('first', 100, ['near'])
        self.assertTrue(decoder.started('first'))
        loader.request('second', 100)
        token, _ = loader.request('first', 100, ['near', 'near', 'first'])

        decoder.release('first')
        self.idle(loader)
        self.assertEqual(loader.take_result()[0], token)
        self.assertEqual(decoder.calls, ['first', 'near'])
        self.assertEqual(self.names(loader), {'first', 'near'})


if __name__ == '__main__':
    unittest.main()
