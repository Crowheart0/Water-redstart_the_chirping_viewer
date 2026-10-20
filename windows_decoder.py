"""Read through installed Windows Imaging Component codecs without extra DLLs."""
import ctypes as C
import sys
import uuid
from PIL import Image


class WICError(OSError):
    pass


class GUID(C.Structure):
    _fields_ = [('data', C.c_ubyte * 16)]

    def __init__(self, value):
        super().__init__()
        self.data[:] = uuid.UUID(value).bytes_le


def decode_windows(path, quality):
    if sys.platform != 'win32':
        raise WICError('Windows codecs are unavailable on this platform')
    ole = C.OleDLL('ole32')
    ole.CoInitializeEx.argtypes = [C.c_void_p, C.c_uint32]
    ole.CoInitializeEx.restype = C.c_long
    initialized = ole.CoInitializeEx(None, 0)
    owned = initialized in (0, 1)
    if initialized < 0 and initialized != -2147417850:
        raise WICError(f'COM initialization failed: {initialized}')
    pointers = []

    def call(pointer, slot, argtypes, *args):
        table = C.cast(pointer, C.POINTER(C.POINTER(C.c_void_p))).contents
        function = C.WINFUNCTYPE(C.c_long, C.c_void_p, *argtypes)(table[slot])
        result = function(pointer, *args)
        if result < 0:
            raise WICError(f'Windows image codec error 0x{result & 0xffffffff:08x}')

    def create(pointer, slot, argtypes=(), args=()):
        output = C.c_void_p()
        call(pointer, slot, list(argtypes) + [C.POINTER(C.c_void_p)], *args, C.byref(output))
        pointers.append(output)
        return output

    try:
        factory = C.c_void_p()
        clsid = GUID('cacaf262-9370-4615-a13b-9f5539da4c0a')
        iid = GUID('ec5ec8a9-c395-4314-9c77-54d7a935ff70')
        ole.CoCreateInstance.argtypes = [C.POINTER(GUID), C.c_void_p, C.c_uint32,
                                         C.POINTER(GUID), C.POINTER(C.c_void_p)]
        ole.CoCreateInstance.restype = C.c_long
        result = ole.CoCreateInstance(C.byref(clsid), None, 1, C.byref(iid), C.byref(factory))
        if result < 0:
            raise WICError(f'Cannot create WIC factory: {result}')
        pointers.append(factory)
        decoder = create(factory, 3, [C.c_wchar_p, C.c_void_p, C.c_uint32, C.c_uint32],
                         [path, None, 0x80000000, 0])
        frame = create(decoder, 13, [C.c_uint32], [0])
        width, height = C.c_uint32(), C.c_uint32()
        call(frame, 3, [C.POINTER(C.c_uint32), C.POINTER(C.c_uint32)], C.byref(width), C.byref(height))
        if not width.value or not height.value:
            raise WICError('Windows codec returned an empty frame')
        source = frame
        ratio = min(1.0, quality / max(width.value, height.value))
        size = (max(1, round(width.value * ratio)), max(1, round(height.value * ratio)))
        if ratio < 1:
            scaler = create(factory, 11)
            call(scaler, 8, [C.c_void_p, C.c_uint32, C.c_uint32, C.c_uint32], frame, *size, 3)
            source = scaler
        rgb = GUID('6fddc324-4e03-4bfe-b185-3d77768dc90d')
        converter = create(factory, 10)
        call(converter, 8, [C.c_void_p, C.POINTER(GUID), C.c_uint32, C.c_void_p, C.c_double, C.c_uint32],
             source, C.byref(rgb), 0, None, 0.0, 0)
        stride = size[0] * 3
        buffer = (C.c_ubyte * (stride * size[1]))()
        call(converter, 7, [C.c_void_p, C.c_uint32, C.c_uint32, C.c_void_p],
             None, stride, len(buffer), buffer)
        image = Image.frombytes('RGB', size, bytes(buffer))
        # WIC exposes the codec's source color space separately from pixels.
        try:
            count = C.c_uint32()
            call(frame, 9, [C.c_uint32, C.c_void_p, C.POINTER(C.c_uint32)], 0, None, C.byref(count))
            if count.value:
                context = create(factory, 15)
                contexts = (C.c_void_p * 1)(context.value)
                call(frame, 9, [C.c_uint32, C.c_void_p, C.POINTER(C.c_uint32)], 1, contexts, C.byref(count))
                kind = C.c_uint32()
                call(context, 6, [C.POINTER(C.c_uint32)], C.byref(kind))
                if kind.value == 1:
                    length = C.c_uint32()
                    call(context, 7, [C.c_uint32, C.c_void_p, C.POINTER(C.c_uint32)], 0, None, C.byref(length))
                    data = (C.c_ubyte * length.value)()
                    call(context, 7, [C.c_uint32, C.c_void_p, C.POINTER(C.c_uint32)], length.value, data, C.byref(length))
                    image.info['icc_profile'] = bytes(data)
                elif kind.value == 2:
                    color = C.c_uint32()
                    call(context, 8, [C.POINTER(C.c_uint32)], C.byref(color))
                    image.getexif()[40961] = color.value
        except WICError:
            pass
        return image
    finally:
        for pointer in reversed(pointers):
            table = C.cast(pointer, C.POINTER(C.POINTER(C.c_void_p))).contents
            C.WINFUNCTYPE(C.c_uint32, C.c_void_p)(table[2])(pointer)
        if owned:
            ole.CoUninitialize()
