"""A tiny migration runner: numbered plain-SQL files in db/migrations/ (NNN_name.sql),
applied in order, each in its own transaction, and recorded in a schema_migrations table.
Chosen over Alembic because there's no ORM and one developer; this module is just SQL files and a loop."""
import os
import re
from typing import NamedTuple

# lowercase only, so 001_Init.sql and 001_init.sql can't both exist on a case-sensitive CI runner
NAME_RE = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")


class Migration(NamedTuple):
    version: str  # "001", kept as text so the leading zeros match schema_migrations.version
    name: str
    path: str


def discover(directory: str) -> list[Migration]:
    """Every NNN_name.sql in directory, sorted by version."""
    # raise rather than return []: a wrong path would otherwise look like "nothing to apply"
    if not os.path.isdir(directory):
        raise ValueError(f"migrations directory not found: {directory}")
    found: dict[str, Migration] = {}
    for fname in os.listdir(directory):
        path = os.path.join(directory, fname)
        if not fname.endswith(".sql") or not os.path.isfile(path):
            continue  # README.md, .gitkeep and the like
        m = NAME_RE.match(fname)
        if not m:
            # a typo'd name skipped silently would never be applied, so fail loudly
            raise ValueError(f"bad migration file name: {fname} (expected NNN_name.sql, lowercase)")
        version, name = m.groups()
        if version in found:
            raise ValueError(f"duplicate migration version {version}: {os.path.basename(found[version].path)} and {fname}")
        found[version] = Migration(version, name, path)
    return [found[v] for v in sorted(found)]
