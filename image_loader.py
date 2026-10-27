"""Image decoding and bounded background prefetching, independent of Tk."""
from collections import OrderedDict, deque
import io
import logging
import os
import threading
import hashlib
import tempfile
import numpy as np

from PIL import Image, ImageCms, ImageOps
import rawpy
from image_render import build_levels

RAW_EXTENSIONS = {'.arw', '.sr2', '.srf', '.crw', '.cr2', '.cr3', '.nef',
                  '.nrw', '.dng', '.orf', '.rw2', '.raf', '.pef'}
SRGB = ImageCms.createProfile('sRGB')


def is_adobe_rgb(image):
    """Recognize explicitly tagged previews without guessing untagged colors."""
    try:
        exif = image.getexif()
        camera = exif.get_ifd(34665) if 34665 in exif else exif
        interop = exif.get_ifd(40965) if 40965 in camera else {}
        return camera.get(40961) == 2 or interop.get(1) in ('R03', b'R03', b'R03\x00')
    except (KeyError, TypeError, ValueError, OSError):
        return False


def adobe_rgb_to_srgb(image):
    """Convert the Adobe RGB (1998) encoding, using D65 primaries and gamma."""
    pixels = np.array(image.convert('RGB'))
    # Work in strips so 8K pictures do not need several full float buffers.
    matrix = np.array([[1.39835574, -0.39835574, 0.0],
                       [0.0, 1.0, 0.0],
                       [0.0, -0.04292899, 1.04292899]], dtype=np.float32)
    for top in range(0, pixels.shape[0], 128):
        strip = pixels[top:top + 128].astype(np.float32) / 255.0
        linear = np.clip((strip ** (563 / 256)) @ matrix.T, 0, 1)
        encoded = np.where(linear <= 0.0031308, linear * 12.92,
                           1.055 * linear ** (1 / 2.4) - 0.055)
        pixels[top:top + 128] = np.rint(encoded * 255).astype(np.uint8)
    return Image.fromarray(pixels)


def prepare_image(image, quality, orientation=None):
    """Detach pixels from the file and convert embedded profiles for Tk display."""
    adobe_rgb = is_adobe_rgb(image)
    image.draft('RGB', (quality, quality))
    has_orientation = image.getexif().get(274, 1) != 1
    if has_orientation:
        image = ImageOps.exif_transpose(image)
    if not has_orientation and orientation:
        # LibRaw flip values; postprocess already applies these automatically.
        operation = {3: Image.Transpose.ROTATE_180,
                     5: Image.Transpose.ROTATE_90,
                     6: Image.Transpose.ROTATE_270}.get(orientation)
        if operation is not None:
            image = image.transpose(operation)
    image.thumbnail((quality, quality), Image.Resampling.LANCZOS, reducing_gap=3.0)
    profile = image.info.get('icc_profile')
    alpha = image.convert('RGBA').getchannel('A') if 'transparency' in image.info or image.mode in ('RGBA', 'LA', 'PA') else None
    if profile:
        try:
            source = ImageCms.ImageCmsProfile(io.BytesIO(profile))
            mode = image.mode if image.mode in ('RGB', 'CMYK', 'LAB', 'L') else 'RGB'
            image = ImageCms.profileToProfile(image.convert(mode), source, SRGB, outputMode='RGB')
        except (ImageCms.PyCMSError, OSError, ValueError):
            logging.warning('Invalid/unsupported ICC profile; displaying RGB pixels')
            image = image.convert('RGB')
    elif adobe_rgb:
        image = adobe_rgb_to_srgb(image)
    else:
        if image.mode != 'RGB':
            image = image.convert('RGB')
    if alpha is not None:
        image.putalpha(alpha)
    image.load()
    return image


def read_image(path, quality):
    if os.path.splitext(path)[1].lower() not in RAW_EXTENSIONS:
        with Image.open(path) as image:
            return prepare_image(image, quality)
    # Older rawpy versions unpack the sensor in imread(), even for previews.
    with rawpy.RawPy() as raw:
        raw.open_file(path)
        try:
            thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                with Image.open(io.BytesIO(thumb.data)) as image:
                    return prepare_image(image, quality, raw.sizes.flip)
            if thumb.format == rawpy.ThumbFormat.BITMAP:
                return prepare_image(Image.fromarray(thumb.data), quality, raw.sizes.flip)
        except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError,
                OSError, ValueError):
            pass
        # Preserve camera white balance and exposure. No camera Picture Control
        # emulation is possible here; the embedded JPEG is preferred for that.
        raw.unpack()
        rgb = raw.postprocess(half_size=quality <= max(raw.sizes.width, raw.sizes.height) // 2,
                              use_camera_wb=True, use_auto_wb=False,
                              output_color=rawpy.ColorSpace.sRGB, output_bps=8,
                              no_auto_bright=True)
        return prepare_image(Image.fromarray(rgb), quality)


def read_for_display(path, quality, raw_mode='windows', monitor_profile=None):
    """Keep the Windows codec and camera preview paths explicitly selectable."""
    raw_file = os.path.splitext(path)[1].lower() in RAW_EXTENSIONS
    decoder = '普通照片'
    cache_path = None
    # Cache the selected display quality too: displayed frames are now ready
    # for Space zoom, not a separate 1600px preview followed by a detail load.
    if raw_file and raw_mode == 'windows':
        try:
            stamp = os.stat(path)
            monitor_stamp = os.stat(monitor_profile).st_mtime_ns if monitor_profile else 0
            fingerprint = repr((os.path.abspath(path), stamp.st_size, stamp.st_mtime_ns,
                                quality, monitor_profile, monitor_stamp, 'wic-preview-v1'))
            directory = os.path.join(tempfile.gettempdir(), 'WaterRedstart', 'preview-v2')
            os.makedirs(directory, exist_ok=True)
            cache_path = os.path.join(directory, hashlib.sha256(fingerprint.encode('utf-8')).hexdigest() + '.bmp')
            if os.path.isfile(cache_path):
                with Image.open(cache_path) as cached:
                    cached.load()
                    image = cached.copy()
                os.utime(cache_path, None)
                image.info['decoder_label'] = 'Windows RAW（缓存）'
                return build_levels(image)
        except OSError:
            cache_path = None
    if raw_file and raw_mode == 'windows':
        from windows_decoder import decode_windows, WICError
        try:
            image = decode_windows(path, quality)
            orientation = None
            try:
                with rawpy.RawPy() as raw:
                    raw.open_file(path)
                    orientation = raw.sizes.flip
            except rawpy.LibRawError:
                pass
            image = prepare_image(image, quality, orientation)
            decoder = 'Windows RAW'
        except WICError:
            image = read_image(path, quality)
            decoder = '系统 RAW 不可用，已回退相机预览'
    else:
        image = read_image(path, quality)
        if raw_file:
            decoder = '相机预览'
    if monitor_profile:
        try:
            image = ImageCms.profileToProfile(image, SRGB, monitor_profile, outputMode=image.mode)
        except (ImageCms.PyCMSError, OSError, ValueError):
            logging.warning('Monitor color conversion unavailable')
    image.info['decoder_label'] = decoder
    if cache_path and decoder == 'Windows RAW':
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=os.path.dirname(cache_path), suffix='.tmp', delete=False) as output:
                temporary = output.name
                image.save(output, format='BMP')
            os.replace(temporary, cache_path)
            temporary = None
            # Evict oldest preview files at 1 GiB; originals are never touched.
            entries = sorted((entry.stat().st_mtime, entry.stat().st_size, entry.path)
                             for entry in os.scandir(os.path.dirname(cache_path))
                             if entry.name.endswith('.bmp'))
            total = sum(size for _, size, _ in entries)
            for _, size, entry_path in entries:
                if total <= 1024 ** 3:
                    break
                if entry_path != cache_path:
                    os.remove(entry_path)
                    total -= size
        except OSError:
            pass
        finally:
            if temporary and os.path.isfile(temporary):
                os.remove(temporary)
    return build_levels(image)


def get_monitor_profile():
    """Return Windows' default display ICC profile, when configured."""
    if os.name != 'nt':
        return None
    import ctypes as C
    user, gdi = C.WinDLL('user32'), C.WinDLL('gdi32')
    user.GetDC.argtypes, user.GetDC.restype = [C.c_void_p], C.c_void_p
    user.ReleaseDC.argtypes = [C.c_void_p, C.c_void_p]
    gdi.GetICMProfileW.argtypes = [C.c_void_p, C.POINTER(C.c_uint32), C.c_wchar_p]
    dc = user.GetDC(None)
    try:
        length = C.c_uint32(32768)
        buffer = C.create_unicode_buffer(length.value)
        if gdi.GetICMProfileW(dc, C.byref(length), buffer):
            return buffer.value
    finally:
        user.ReleaseDC(None, dc)
    return None


class ImageLoader:
    """Decoders prioritize the latest request and share bounded prefetch results."""
    def __init__(self, decoder=read_image, workers=1, *,
                 budget_bytes=256 * 1024 * 1024,
                 low_memory_budget_bytes=96 * 1024 * 1024,
                 estimate_prefetch=False):
        for name, value in (('budget_bytes', budget_bytes),
                            ('low_memory_budget_bytes', low_memory_budget_bytes)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f'{name} must be a positive integer')
        self.decoder = decoder
        self.condition = threading.Condition()
        self.cache = OrderedDict()
        self.cache_bytes = 0
        self.normal_budget = budget_bytes
        self.low_memory_budget = low_memory_budget_bytes
        self.budget = self.normal_budget
        self.estimate_prefetch = estimate_prefetch
        self.generation = 0
        self.request_id = 0
        self.pending = None
        self.prefetch = deque()
        self.desired = {}
        self.known_sizes = {}
        self.result = None
        self.closed = False
        self.in_flight = set()
        self.threads = [threading.Thread(target=self._run, daemon=True, name=f'photo-decoder-{index}')
                        for index in range(workers)]
        self.thread = self.threads[0]
        for thread in self.threads:
            thread.start()

    @staticmethod
    def _bytes(image):
        # Pillow RGB cores use four bytes per pixel, despite three bands.
        return image.width * image.height * 4 + sum(level.width * level.height * 4
            for _, level in image.info.get('display_levels', ()))

    def reset(self):
        with self.condition:
            self.generation += 1
            self.pending = self.result = None
            self.prefetch.clear()
            self.desired.clear()
            self.known_sizes.clear()
            self.cache.clear()
            self.cache_bytes = 0
            self.condition.notify_all()

    def request(self, path, quality, neighbors=(), low_memory=False):
        key = (os.path.abspath(path), quality)
        with self.condition:
            self.request_id += 1
            token = self.request_id
            self.result = None
            self.budget = self.low_memory_budget if low_memory else self.normal_budget
            # Order is the latest browsing priority, independent of which
            # decoder finishes first. Include the foreground only once.
            window = dict.fromkeys([key, *((os.path.abspath(p), quality) for p in neighbors)])
            self.desired = {item: rank for rank, item in enumerate(window)}
            self.known_sizes = {item: size for item, size in self.known_sizes.items()
                                if item in self.desired}
            self._trim()
            self.prefetch = deque(item for item in self.desired
                                  if item != key and item not in self.cache
                                  and self._can_fit(item, self._estimated_bytes(item)))
            image = self.cache.get(key)
            if image is not None:
                self.cache.move_to_end(key)
                self.pending = None
            else:
                self.pending = (key, token, self.generation)
            self.condition.notify_all()
            return token, image

    def take_result(self):
        with self.condition:
            result, self.result = self.result, None
            return result

    def close(self):
        with self.condition:
            self.closed = True
            self.cache.clear()
            self.cache_bytes = 0
            self.prefetch.clear()
            self.desired.clear()
            self.known_sizes.clear()
            self.pending = self.result = None
            self.condition.notify_all()

    def _trim(self, required=0, priority=None):
        for key in list(self.cache):
            if key not in self.desired:
                self.cache_bytes -= self._bytes(self.cache.pop(key))
        # Keep the foreground and closest neighbors even after a budget
        # reduction, or when a slower near frame arrives after a far frame.
        for key in sorted(self.cache, key=self.desired.__getitem__, reverse=True):
            if self.cache_bytes + required <= self.budget:
                break
            if priority is not None and self.desired[key] <= priority:
                break
            self.cache_bytes -= self._bytes(self.cache.pop(key))

    def _can_fit(self, key, size):
        priority = self.desired[key]
        protected = sum(self._bytes(image) for item, image in self.cache.items()
                        if self.desired[item] < priority)
        return protected + size <= self.budget

    def _estimated_bytes(self, key):
        """Estimate speculation only; decoded pixels always use their real size."""
        if key in self.known_sizes:
            return self.known_sizes[key]
        if self.estimate_prefetch and self.known_sizes:
            current = next(iter(self.desired), None)
            if current in self.known_sizes:
                return self.known_sizes[current]
            return max(self.known_sizes.values())
        return 1

    def _run(self):
        while True:
            with self.condition:
                while True:
                    if self.closed:
                        return
                    if self.pending is not None and (self.pending[2], self.pending[0]) not in self.in_flight:
                        key, token, generation = self.pending
                        self.pending = None
                    elif self.prefetch:
                        key, token, generation = self.prefetch.popleft(), None, self.generation
                        if key in self.cache or (generation, key) in self.in_flight:
                            continue
                        # A discarded far frame may still be in the window.
                        # Remember its size, not its pixels, so repeated cache
                        # hits do not repeatedly decode a frame that cannot fit.
                        if not self._can_fit(key, self._estimated_bytes(key)):
                            continue
                    else:
                        self.condition.wait()
                        continue
                    self.in_flight.add((generation, key))
                    decoder = self.decoder
                    break
            image, error = None, None
            try:
                image = decoder(*key)
            except Exception as exc:
                error = str(exc)
            with self.condition:
                self.in_flight.discard((generation, key))
                self.condition.notify_all()
                if self.closed:
                    return
                if generation != self.generation or key not in self.desired:
                    image = None
                    continue
                # A prefetch can become the requested photo while decoding.
                if self.pending is not None and self.pending[0] == key:
                    _, token, _ = self.pending
                    self.pending = None
                if image is not None:
                    size = self._bytes(image)
                    self.known_sizes[key] = size
                    if self._can_fit(key, size):
                        self._trim(size, priority=self.desired[key])
                        self.cache[key] = image
                        self.cache_bytes += size
                if token == self.request_id:
                    self.result = (token, image, error)
                # An idle decoder must not retain an evicted image via its
                # local variable after the rolling window has moved on.
                image = None
