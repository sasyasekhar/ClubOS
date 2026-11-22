from app.config.settings import settings
from app.services.storage.base import (
    AttachmentInventory,
    AttachmentStorage,
    ObjectMissing,
    StorageError,
)
from app.services.storage.local import LocalAttachmentStorage

__all__ = [
    "AttachmentInventory",
    "AttachmentStorage",
    "ObjectMissing",
    "StorageError",
    "get_attachment_storage",
]


def get_attachment_storage() -> AttachmentInventory:
    if settings.ATTACHMENT_STORAGE_BACKEND == "local" and settings.APP_ENV in (
        "development",
        "test",
    ):
        return LocalAttachmentStorage(settings.ATTACHMENT_STORAGE_DIRECTORY)

    raise StorageError("A private production attachment storage provider is required.")
