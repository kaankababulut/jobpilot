"""Shared test setup: import path, config fixture and a job factory."""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)  # so tests can import job_searcher and jobpilot without installing anything


@pytest.fixture(scope="module")
def cfg():
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        return json.load(f)


# a plain function, not a fixture, because parametrize lists call it at collection time
def make_job(**kw):
    job = {"id": "1", "title": "Junior Python Developer", "companyName": "Acme",
           "location": "Istanbul, Türkiye", "descriptionText": "Python and SQL.",
           "seniorityLevel": "Entry level"}
    job.update(kw)
    return job


@pytest.fixture
def pg():
    """A connection whose search_path points at a throwaway schema, so tests never touch public tables."""
    import uuid
    from dotenv import load_dotenv
    from jobpilot.db import connect
    load_dotenv(os.path.join(ROOT, ".env"))
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set")
    try:
        conn = connect(url)
    except Exception as e:  # container not running: skip, don't fail
        pytest.skip(f"database not reachable ({type(e).__name__})")
    conn.autocommit = True  # each statement is its own transaction, so now() moves between loads
    schema = f"test_{uuid.uuid4().hex[:12]}"
    try:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"SET search_path TO {schema}, public")  # public holds the vector extension
        with open(os.path.join(ROOT, "db", "init", "001_schema.sql"), encoding="utf-8") as f:
            conn.execute(f.read())
        yield conn
    finally:
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.close()
