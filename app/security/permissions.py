
from uuid import UUID

from fastapi import Depends, HTTPException, status

from app.models.account import Account
from app.models.enums import AccountType
from app.routes.auth import require_account


def require_roles(*allowed_roles: AccountType):
    """Restrict a route to specified account roles."""

    def check_role(
        account: Account = Depends(require_account),
    ) -> Account:
        if account.account_type not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Permission denied.",
            )

        return account

    return check_role


def ensure_same_school(
    account: Account,
    school_id: UUID,
) -> None:
    """Prevent access to another school's resources."""

    if account.school_id is None or account.school_id != school_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Resource not found.",
        )
