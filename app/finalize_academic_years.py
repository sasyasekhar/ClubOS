import argparse
import sys

from sqlalchemy.exc import SQLAlchemyError

from app.services.academic_year_closure_finalization import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_WINDOW_LIMIT,
    process_expired_closure_windows,
)
from app.services.maintenance import MaintenanceError, exclusive_maintenance


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Process expired academic-year completion windows.")
    parser.add_argument("--limit", type=int, default=DEFAULT_WINDOW_LIMIT, help="Maximum windows per run.")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="Maximum revisions per window.")
    args = parser.parse_args(argv)

    try:
        with exclusive_maintenance() as db:
            windows, rejected = process_expired_closure_windows(db, limit=args.limit, batch_size=args.batch_size)
    except ValueError as exc:
        parser.error(str(exc))
    except MaintenanceError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except SQLAlchemyError as exc:
        print(f"Academic-year finalization failed ({type(exc).__name__}).", file=sys.stderr)
        return 1

    print(f"Examined {windows} expired windows and recorded {rejected} automatic rejections.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
