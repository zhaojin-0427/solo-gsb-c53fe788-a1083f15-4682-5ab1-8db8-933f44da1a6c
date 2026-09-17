"""Database engine, session factory and schema bootstrap (incl. immutability
triggers)."""

from __future__ import annotations

import logging
import os
import time

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .models import Base, PriceVersion

log = logging.getLogger("billing.db")

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://billing:billing@localhost:5432/billing",
)

engine = create_engine(DATABASE_URL, pool_size=10, max_overflow=20, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Append-only enforcement: the ledger, bills and price versions can be inserted
# but never updated or deleted. Corrections/reversals are new rows by design.
_IMMUTABLE_TABLES = (
    "usage_records",
    "bills",
    "bill_lines",
    "price_versions",
    "price_tiers",
)

_TRIGGER_SQL = """
CREATE OR REPLACE FUNCTION reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'immutable ledger: rows of % cannot be updated or deleted', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;
"""


def _trigger_ddl(table: str) -> str:
    return f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = '{table}_immutable') THEN
        CREATE TRIGGER {table}_immutable
        BEFORE UPDATE OR DELETE ON {table}
        FOR EACH ROW EXECUTE FUNCTION reject_mutation();
    END IF;
END $$;
"""


def _btree_gist_available() -> bool:
    """The official postgres image ships the contrib module; minimal installs
    may not. Non-overlap is still enforced at the app level (plan row lock +
    overlap query) — the exclusion constraint is defense in depth."""
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
        return True
    except SQLAlchemyError:
        log.warning("btree_gist unavailable: price-version exclusion constraint "
                    "will not be installed (app-level checks still apply)")
        return False


def init_db(retries: int = 30, delay: float = 1.0) -> None:
    last_exc: Exception | None = None
    for _ in range(retries):
        try:
            if not _btree_gist_available():
                table = PriceVersion.__table__
                for constraint in list(table.constraints):
                    if constraint.name == "excl_price_versions_no_overlap":
                        table.constraints.discard(constraint)
            Base.metadata.create_all(engine)
            with engine.begin() as conn:
                conn.execute(text(_TRIGGER_SQL))
                for table in _IMMUTABLE_TABLES:
                    conn.execute(text(_trigger_ddl(table)))
            return
        except OperationalError as exc:  # DB not ready yet (compose startup)
            last_exc = exc
            time.sleep(delay)
    raise RuntimeError(f"database not reachable after {retries} attempts: {last_exc}")
