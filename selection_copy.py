"""Copy one selected photo group without acknowledging partial success.

This module has no Tk dependencies. All paths are snapshotted by the UI.
"""
import errno
import filecmp
import os
import shutil


LOW_SPACE_BYTES = 256 * 1024 * 1024


class InsufficientSpace(OSError):
    def __init__(self, destination, required, available):
        self.required = required
        self.available = available
        super().__init__(
            errno.ENOSPC,
            f"目标文件夹空间不足。\n目标：{destination}\n"
            f"本组选片需要：{required / 1024**2:.2f} MiB（{required:,} 字节）\n"
            f"当前可用：{available / 1024**2:.2f} MiB（{available:,} 字节）",
        )


def copy_selection(sources, destination):
    """Return only newly created files, so undo never deletes earlier selections.

    Existing byte-identical files are already selected. Conflicting names are
    rejected, and exclusive creation also protects against a concurrent writer.
    Any write failure rolls back this operation's files, including partial files.
    """
    os.makedirs(destination, exist_ok=True)
    pending = []
    required = 0
    for source in sources:
        target = os.path.join(destination, os.path.basename(source))
        if os.path.lexists(target):
            if os.path.isfile(target) and filecmp.cmp(source, target, shallow=False):
                continue
            raise FileExistsError(
                errno.EEXIST, "目标中存在同名但内容不同的文件，请更换目标文件夹", target)
        required += os.path.getsize(source)
        pending.append((source, target))

    free = shutil.disk_usage(destination).free
    if required > free:
        raise InsufficientSpace(destination, required, free)

    created = []
    try:
        for source, target in pending:
            with open(source, "rb") as incoming, open(target, "xb") as outgoing:
                created.append(target)
                shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            shutil.copystat(source, target)
    except Exception as error:
        leftovers = []
        for target in reversed(created):
            try:
                os.remove(target)
            except FileNotFoundError:
                pass
            except OSError:
                leftovers.append(target)
        if leftovers:
            raise OSError(
                getattr(error, "errno", None),
                f"{error}\n以下未完成文件无法清理，请检查后删除：\n" + "\n".join(leftovers),
            ) from error
        raise
    # A failed post-copy capacity query does not invalidate completed copies.
    try:
        free = shutil.disk_usage(destination).free
    except OSError:
        free = None
    return created, free
