import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config.settings import settings
from app.main import app
from app.services.storage import ObjectMissing, StorageError, get_attachment_storage
from app.services.storage.local import LocalAttachmentStorage


@pytest.fixture
def private_root(tmp_path: Path) -> Path:
    # macOS's /var and /tmp can be aliases, so configure the real directory.
    return tmp_path.resolve() / "private"


def test_local_round_trip_and_private_permissions(private_root: Path):
    storage = LocalAttachmentStorage(private_root)
    assert not private_root.exists()
    storage.put("activities/revision/object", b"first", "application/pdf")
    storage.put("activities/revision/object", b"second", "application/pdf")

    stored = private_root / "activities/revision/object"
    assert stored.stat().st_mode & 0o777 == 0o600
    for directory in (private_root, stored.parent.parent, stored.parent):
        assert directory.stat().st_mode & 0o777 == 0o700
    with storage.get("activities/revision/object") as content:
        assert content.read(100) == b"second"

    storage.delete("activities/revision/object")
    storage.delete("activities/revision/object")
    with pytest.raises(ObjectMissing), storage.get("activities/revision/object"):
        pytest.fail("Object should have been removed")


@pytest.mark.parametrize(
    "key",
    ("", "/absolute/file", "../outside", "abc/../../outside", "abc\\outside", "a//b", "a/./b"),
)
def test_local_rejects_unsafe_keys(private_root: Path, key: str):
    storage = LocalAttachmentStorage(private_root)
    with pytest.raises(StorageError):
        storage.put(key, b"file", "application/pdf")
    with pytest.raises(StorageError), storage.get(key):
        pytest.fail("Unsafe key was opened")
    with pytest.raises(StorageError):
        storage.delete(key)
    assert not private_root.exists()


def test_local_rejects_symlinked_directories(private_root: Path):
    outside = private_root.parent / "outside"
    outside.mkdir(mode=0o700)
    sentinel = outside / "object"
    sentinel.write_bytes(b"unrelated data")
    private_root.symlink_to(outside, target_is_directory=True)
    storage = LocalAttachmentStorage(private_root)
    with pytest.raises(StorageError):
        storage.put("object", b"overwrite", "application/pdf")

    private_root.unlink()
    private_root.mkdir(mode=0o700)
    (private_root / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(StorageError):
        storage.put("escape/object", b"overwrite", "application/pdf")
    with pytest.raises(StorageError), storage.get("escape/object"):
        pytest.fail("Symlink was followed")
    with pytest.raises(StorageError):
        storage.delete("escape/object")
    assert sentinel.read_bytes() == b"unrelated data"


def test_local_leaf_symlink_does_not_expose_or_overwrite_target(private_root: Path):
    private_root.mkdir(mode=0o700)
    outside_file = private_root.parent / "outside.pdf"
    outside_file.write_bytes(b"unrelated data")
    (private_root / "object").symlink_to(outside_file)
    storage = LocalAttachmentStorage(private_root)

    with pytest.raises(StorageError), storage.get("object"):
        pytest.fail("Symlink was followed")
    storage.put("object", b"replacement", "application/pdf")
    with storage.get("object") as content:
        assert content.read(100) == b"replacement"
    assert outside_file.read_bytes() == b"unrelated data"


def test_local_rejects_hard_links(private_root: Path):
    private_root.mkdir(mode=0o700)
    outside_file = private_root.parent / "outside.pdf"
    outside_file.touch(mode=0o600)
    (private_root / "object").hardlink_to(outside_file)
    storage = LocalAttachmentStorage(private_root)
    with pytest.raises(StorageError), storage.get("object"):
        pytest.fail("Hard-linked file was opened")


def test_local_rejects_directory_objects(private_root: Path):
    private_root.mkdir(mode=0o700)
    (private_root / "object").mkdir(mode=0o700)
    storage = LocalAttachmentStorage(private_root)
    with pytest.raises(StorageError), storage.get("object"):
        pytest.fail("Directory was opened as file")


def test_local_inventory_lists_finalized_keys_under_prefix(private_root: Path):
    storage = LocalAttachmentStorage(private_root)
    assert list(storage.iter_keys("activities")) == []
    storage.put("activities/revision/object", b"file", "application/pdf")
    storage.put("other/files/object", b"file", "application/pdf")
    (private_root / "activities/revision/.upload-incomplete.tmp").touch()
    (private_root / "activities/link").symlink_to(private_root / "other")
    assert list(storage.iter_keys("activities")) == ["activities/revision/object"]


def test_local_refuses_public_or_permissive_directories(private_root: Path):
    with pytest.raises(StorageError):
        LocalAttachmentStorage(Path("app/static/attachments"))
    private_root.mkdir()
    private_root.chmod(0o755)
    with pytest.raises(StorageError):
        LocalAttachmentStorage(private_root).put("object", b"file", "application/pdf")


@pytest.mark.parametrize("unsafe_path", ["app/STATIC/attachments", "APP"])
def test_local_rejects_case_aliases_of_public_paths(
    private_root: Path, monkeypatch: pytest.MonkeyPatch, unsafe_path: str
):
    monkeypatch.chdir(private_root.parent)
    Path("app").mkdir(mode=0o700)
    static = Path("app/static")
    static.mkdir(mode=0o700)
    real_open = os.open

    # Emulate APFS case-insensitive lookup when the test runs on a sensitive filesystem.
    def open_alias(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is not None and path in ("STATIC", "APP"):
            path = path.lower()
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", open_alias)
    with pytest.raises(StorageError, match="requires a private directory"):
        LocalAttachmentStorage(Path(unsafe_path)).put("object", b"file", "application/pdf")
    assert list(Path("app").iterdir()) == [static]
    assert list(static.iterdir()) == []


def test_local_retries_directory_sync_after_unlink(
    private_root: Path, monkeypatch: pytest.MonkeyPatch
):
    storage = LocalAttachmentStorage(private_root)
    storage.put("object", b"file", "application/pdf")
    real_fsync = os.fsync
    calls = 0

    def fail_first_sync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("Directory sync failed")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_first_sync)
    with pytest.raises(StorageError):
        storage.delete("object")
    assert not (private_root / "object").exists()
    storage.delete("object")
    assert calls == 2


def test_production_startup_rejects_local_storage(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "APP_ENV", "production")
    with pytest.raises(StorageError, match="private production attachment storage provider"):
        get_attachment_storage()
    with pytest.raises(StorageError, match="private production attachment storage provider"):
        with TestClient(app):
            pytest.fail("Production started with local attachment storage")
