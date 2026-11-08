"""Apply the initial schema; future changes must add numbered migrations."""
from sqlalchemy import text

from .db import Base, engine
from . import models  # noqa: F401


def main():
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)"))
        version = connection.scalar(text("SELECT MAX(version) FROM schema_migrations")) or 0
        if version > 1:
            raise RuntimeError("Database schema is newer than this application")
        if version == 0:
            Base.metadata.create_all(connection)
            connection.execute(text("INSERT INTO schema_migrations (version) VALUES (1)"))


if __name__ == "__main__":
    main()
