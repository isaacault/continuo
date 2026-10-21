"""Numbered SQL migrations (continuo/migrations/NNNN_name.sql), applied in order, each in its own
transaction, recorded in schema_migrations. Replaces lidseeker's inline ALTER TABLE checks."""
import logging
import os
import re
from importlib import resources

from .. import config
from .conn import connect, now

log = logging.getLogger("continuo")

_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def available() -> list[tuple[int, str, str]]:
    out = []
    for entry in resources.files("continuo").joinpath("migrations").iterdir():
        m = _NAME.match(entry.name)
        if m:
            out.append((int(m.group(1)), entry.name, entry.read_text()))
    return sorted(out)


def applied() -> set[int]:
    with connect() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        return {r[0] for r in c.execute("SELECT version FROM schema_migrations")}


def run() -> list[str]:
    """Apply pending migrations. Returns the names applied."""
    directory = os.path.dirname(config.settings.db_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with connect() as c:
        # Persistent DB property: readers never block the single writer.
        c.execute("PRAGMA journal_mode=WAL")
    done = applied()
    ran = []
    for version, name, sql in available():
        if version in done:
            continue
        with connect() as c:
            # executescript commits implicitly; wrap so a failing migration leaves nothing behind.
            c.executescript(
                "BEGIN;\n" + sql + "\nINSERT INTO schema_migrations (version, name, applied_at) "
                f"VALUES ({version}, '{name}', '{now()}');\nCOMMIT;"
            )
        log.info("applied migration %s", name)
        ran.append(name)
    return ran
