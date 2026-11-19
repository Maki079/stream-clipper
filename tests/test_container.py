import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from stream_clipper import container


def test_prepare_directory_repairs_existing_files_without_following_links(tmp_path, monkeypatch):
    root = tmp_path / "data"
    nested = root / "work"
    nested.mkdir(parents=True)
    secret = nested / "config.yaml"
    secret.write_text("private contents")
    link = root / "external-link"
    link.write_text("symlink placeholder")
    real_lstat = Path.lstat

    def lstat(path):
        if path == root:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o500, st_uid=0, st_gid=0)
        if path == secret:
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o400, st_uid=0, st_gid=0)
        if path == link:
            return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=0, st_gid=0)
        return real_lstat(path)

    ownership, modes = [], []
    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(container.os, "chown", lambda p, u, g, **kw: ownership.append((p, u, g, kw)), raising=False)
    monkeypatch.setattr(container.os, "chmod", lambda p, m, **kw: modes.append((p, m, kw)))
    container.prepare_directory(root)
    assert any(p == secret and (u, g) == (1000, 1000) for p, u, g, _ in ownership)
    assert all(p != link and kw == {"follow_symlinks": False} for p, _, _, kw in ownership)
    assert (secret, 0o600, {"follow_symlinks": False}) in modes
    assert (root, 0o700, {"follow_symlinks": False}) in modes
    assert all(p != link for p, _, _ in modes)
    assert secret.read_text() == "private contents"


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX ownership and symbolic links")
def test_permission_repair_does_not_touch_symlink_targets(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private").write_text("keep")
    root = tmp_path / "data"
    root.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    previous = (outside / "private").stat()
    container.prepare_directory(root, uid=os.getuid(), gid=os.getgid())
    assert (outside / "private").stat() == previous
    with pytest.raises(RuntimeError, match="not a symbolic link"):
        container.prepare_directory(root / "link")


@pytest.mark.parametrize("root", [True, False])
def test_entrypoint_drops_privileges_before_launching_app(monkeypatch, root):
    calls = []
    monkeypatch.setattr(container.os, "geteuid", lambda: 0 if root else 1000, raising=False)
    monkeypatch.setattr(container, "prepare_directory", lambda path: calls.append(("prepare", str(path))))
    for method in ("setgroups", "setgid", "setuid"):
        monkeypatch.setattr(container.os, method, lambda value, method=method: calls.append((method, value)), raising=False)
    monkeypatch.setenv("CLIPPER_WEB_CONTAINER", "")
    for key, value in {"HOME": "/root", "USER": "root", "LOGNAME": "root",
                       "XDG_CONFIG_HOME": "/root/.config", "XDG_CACHE_HOME": "/root/.cache",
                       "XDG_DATA_HOME": "/root/.local/share"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(container.sys, "argv", ["entrypoint", "web", "--autostart"])

    def execute(command, args):
        assert os.environ["CLIPPER_WEB_CONTAINER"] == "true"
        assert os.environ["HOME"] == "/home/clipper"
        assert os.environ["USER"] == os.environ["LOGNAME"] == "clipper"
        assert os.environ["XDG_CONFIG_HOME"] == "/home/clipper/.config"
        assert os.environ["XDG_CACHE_HOME"] == "/home/clipper/.cache"
        assert os.environ["XDG_DATA_HOME"] == "/home/clipper/.local/share"
        calls.append(("exec", command, args))
        raise RuntimeError("exec intercepted")

    monkeypatch.setattr(container.os, "execvp", execute)
    with pytest.raises(RuntimeError, match="exec intercepted"):
        container.main()
    expected = [("prepare", str(Path(path))) for path in container.WRITABLE_DIRS] if root else []
    if root:
        expected += [("setgroups", []), ("setgid", 1000), ("setuid", 1000)]
    assert calls == expected + [("exec", "stream-clipper", ["stream-clipper", "web", "--autostart"])]


def test_unwritable_mount_fails_before_app_start(monkeypatch):
    monkeypatch.setattr(container.os, "geteuid", lambda: 0, raising=False)

    def fail(path):
        raise PermissionError("read-only mount")

    monkeypatch.setattr(container, "prepare_directory", fail)
    with pytest.raises(SystemExit, match="Cannot prepare /config.*read-only mount"):
        container.main()


@pytest.mark.parametrize("value", [None, "", "relative/config", "/root", "/root/.config"])
def test_default_or_root_xdg_paths_use_application_home(monkeypatch, value):
    for key in ("HOME", "USER", "LOGNAME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
        monkeypatch.setenv(key, "")  # track even initially absent keys for teardown
        monkeypatch.delenv(key, raising=False)
    if value is not None:
        monkeypatch.setenv("XDG_CONFIG_HOME", value)
    container.configure_user_environment()
    assert os.environ["HOME"] == "/home/clipper"
    assert os.environ["XDG_CONFIG_HOME"] == "/home/clipper/.config"


def test_explicit_non_root_xdg_paths_are_preserved(monkeypatch):
    values = {"HOME": "/root", "USER": "root", "LOGNAME": "root",
              "XDG_CONFIG_HOME": "/config/third-party", "XDG_CACHE_HOME": "/data/cache",
              "XDG_DATA_HOME": "/data/share"}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    container.configure_user_environment()
    for key in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
        assert os.environ[key] == values[key]
    assert os.environ["HOME"] == "/home/clipper"
