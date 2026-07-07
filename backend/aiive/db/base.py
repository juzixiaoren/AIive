from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from aiive.config import settings

engine = create_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine)


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_all():
    """Create all tables. Used by migration setup and tests."""
    from aiive.db.models import Base  # noqa: F811

    Base.metadata.create_all(bind=engine)
