from uuid import UUID

from sqlalchemy import Text, Uuid, cast, func, literal, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.models.operation_receipt import OperationReceipt


def _fingerprint(db: Session, operation_id: UUID, payload: object) -> bytes:
    # PostgreSQL's canonical JSONB text is shared with the historical backfill.
    key = func.uuid_send(literal(operation_id, Uuid()))
    content = func.convert_to(cast(literal(payload, JSONB()), Text), "UTF8")
    return db.execute(select(func.sha256(key.op("||")(content)))).scalar_one()


def find_receipt(db: Session, operation_id: UUID, action: str, payload: object, error) -> OperationReceipt | None:
    receipt = db.get(OperationReceipt, operation_id)
    if receipt is not None and (receipt.action != action or receipt.fingerprint != _fingerprint(db, operation_id, payload)):
        raise error(409, "Idempotency key reused with different data or unavailable history.")
    return receipt


def stage_receipt(db: Session, operation_id: UUID, action: str, payload: object, *, result_id: UUID | None = None) -> None:
    db.add(OperationReceipt(
        id=operation_id, action=action, fingerprint=_fingerprint(db, operation_id, payload), result_id=result_id,
    ))
