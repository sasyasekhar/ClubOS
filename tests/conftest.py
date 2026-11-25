import pytest

from app.main import app
from app.services.attachment_scanning import get_attachment_scanner
from attachment_samples import CleanAttachmentScanner


@pytest.fixture(autouse=True)
def attachment_scanner(monkeypatch):
    scanner = CleanAttachmentScanner()
    monkeypatch.setitem(app.dependency_overrides, get_attachment_scanner, lambda: scanner)
    return scanner
