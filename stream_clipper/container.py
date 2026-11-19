"""Docker startup: repair bind mount permissions, then run as UID/GID 1000."""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

WRITABLE_DIRS = ("/config", "/data", "/buffer", "/models", "/output")
APP_UID = APP_GID = 1000


def prepare_directory(path: Path, uid: int = APP_UID, gid: int = APP_GID) -> None:
    """Repair only actual files/directories; never follow mounted symlinks."""
    if path.is_symlink():
        raise RuntimeError(f"Mount {path} as a directory, not a symbolic link")
    path.mkdir(parents=True, exist_ok=True)

    def repair(entry: Path) -> None:
        info = entry.lstat()
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            return
        if (info.st_uid, info.st_gid) != (uid, gid):
            os.chown(entry, uid, gid, follow_symlinks=False)
        required = stat.S_IRUSR | stat.S_IWUSR
        if stat.S_ISDIR(info.st_mode):
            required |= stat.S_IXUSR
        mode = stat.S_IMODE(info.st_mode)
        if mode & required != required:
            os.chmod(entry, mode | required, follow_symlinks=False)

    def fail(error):
        raise error

    repair(path)
    for root, dirs, filenames in os.walk(path, followlinks=False, onerror=fail):
        for name in dirs + filenames:
            repair(Path(root) / name)


def main() -> None:
    if os.geteuid() == 0:
        print("Preparing application directory permissions for UID/GID 1000:1000…", flush=True)
        for directory in WRITABLE_DIRS:
            try:
                prepare_directory(Path(directory))
            except (OSError, RuntimeError) as exc:
                raise SystemExit(f"Cannot prepare {directory} for UID/GID 1000:1000: {exc}. "
                                 "Check that the bind mount is writable and supports ownership changes.") from None
        os.setgroups([])
        os.setgid(APP_GID)
        os.setuid(APP_UID)
    # This is set only by the Docker entrypoint, not by ordinary local web runs.
    os.environ["CLIPPER_WEB_CONTAINER"] = "true"
    os.execvp("stream-clipper", ["stream-clipper", *sys.argv[1:]])


if __name__ == "__main__":
    main()
