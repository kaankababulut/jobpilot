"""The pg fixture's guard: skip locally, fail in CI, so a broken CI database can't show green."""
import pytest

from conftest import db_unavailable


def test_skips_by_default(monkeypatch):
    monkeypatch.delenv("JOBPILOT_REQUIRE_DB", raising=False)
    with pytest.raises(pytest.skip.Exception, match="DATABASE_URL not set"):
        db_unavailable("DATABASE_URL not set")


def test_fails_when_db_required(monkeypatch):
    monkeypatch.setenv("JOBPILOT_REQUIRE_DB", "1")
    with pytest.raises(pytest.fail.Exception, match="database not reachable"):
        db_unavailable("database not reachable (OperationalError)")


@pytest.mark.parametrize("value", ["0", "", "true"])
def test_only_exactly_1_requires_db(monkeypatch, value):
    monkeypatch.setenv("JOBPILOT_REQUIRE_DB", value)
    with pytest.raises(pytest.skip.Exception):
        db_unavailable("DATABASE_URL not set")
