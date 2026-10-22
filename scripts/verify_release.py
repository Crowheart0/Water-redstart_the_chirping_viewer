"""Check the v4.0 Windows artifact's icon, metadata, and bundled application."""
import hashlib
import marshal
from pathlib import Path
import struct
import sys
import types

import pefile
from PyInstaller.archive.readers import CArchiveReader


ROOT = Path(__file__).resolve().parents[1]


def constants(code):
    for value in code.co_consts:
        if isinstance(value, types.CodeType):
            yield from constants(value)
        elif isinstance(value, str):
            yield value


def main():
    executable = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "_local" / "releases" / "v4.0.exe"
    archive = CArchiveReader(str(executable))
    icon = (ROOT / "bird.ico").read_bytes()
    assert archive.extract("bird.ico") == icon, "Runtime bird.ico differs"
    code = marshal.loads(archive.extract("photo_viewer"))
    texts = list(constants(code))
    assert "4.0" in texts, "Incorrect application version"
    assert any("绿羊" in text and "Mr.Nobody" in text for text in texts), "Credits missing"
    assert any("目标文件夹空间不足" in text for text in texts), "Space warning missing"
    for text in ("选择选片目标文件夹", "使用 SELECT", "保存 RAW", "保存 JPEG", "音效"):
        assert text in texts, f"New UI feature missing: {text}"
    pyz = archive.open_embedded_archive("PYZ.pyz")
    for name in ("selection_copy", "sound_effects", "image_loader", "image_render", "windows_decoder"):
        assert name in pyz.toc, f"Missing bundled module: {name}"
    assert any("_tcl_data/init.tcl" == name.replace("\\", "/") for name in archive.toc), "Tcl runtime missing"

    with pefile.PE(str(executable)) as pe:
        assert pe.FILE_HEADER.Machine == 0x8664, "Not Windows x64"
        metadata = {
            key.decode(): value.decode()
            for entries in pe.FileInfo for entry in entries
            if hasattr(entry, "StringTable")
            for table in entry.StringTable for key, value in table.entries.items()
        }
        assert metadata["FileVersion"] == "4.0", metadata
        assert metadata["ProductVersion"] == "4.0", metadata
        resources = []
        for entry in pe.DIRECTORY_ENTRY_RESOURCE.entries:
            if entry.id == 3:  # RT_ICON
                for item in entry.directory.entries:
                    for language in item.directory.entries:
                        data = language.data.struct
                        resources.append(pe.get_data(data.OffsetToData, data.Size))
        count = struct.unpack_from("<H", icon, 4)[0]
        expected = []
        for index in range(count):
            size, offset = struct.unpack_from("<II", icon, 6 + index * 16 + 8)
            expected.append(icon[offset:offset + size])
        hashes = lambda values: sorted(hashlib.sha256(value).hexdigest() for value in values)
        assert hashes(resources) == hashes(expected), "EXE icon does not match bird.ico"
    print(f"Verified: Windows x64, v4.0, credits, space warning, destination prompt, RAW/JPEG, sound, 5 app modules, Tcl runtime, {count} matching icon sizes")
    print(f"Bytes: {executable.stat().st_size}")
    print(f"SHA256: {hashlib.sha256(executable.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    main()
