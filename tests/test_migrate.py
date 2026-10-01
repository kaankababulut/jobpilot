import pytest

from jobpilot.migrate import Migration, discover


def _touch(directory, *names):
    for n in names:
        (directory / n).write_text("SELECT 1;")


def test_sorted_by_version_regardless_of_creation_order(tmp_path):
    _touch(tmp_path, "010_later.sql", "002_add_index.sql", "001_initial.sql")
    assert [m.version for m in discover(str(tmp_path))] == ["001", "002", "010"]


def test_version_name_and_path_parsed(tmp_path):
    _touch(tmp_path, "001_initial_schema.sql")
    assert discover(str(tmp_path)) == [Migration("001", "initial_schema", str(tmp_path / "001_initial_schema.sql"))]


def test_non_sql_files_ignored(tmp_path):
    _touch(tmp_path, "001_initial.sql", "README.md", ".gitkeep", "001_initial.sql.bak")
    assert [m.name for m in discover(str(tmp_path))] == ["initial"]


def test_directory_named_like_sql_ignored(tmp_path):
    (tmp_path / "old.sql").mkdir()
    assert discover(str(tmp_path)) == []


@pytest.mark.parametrize("bad", ["1_initial.sql", "001-initial.sql", "001_Initial.sql", "initial.sql", "0001_x.sql"])
def test_badly_named_sql_raises_naming_the_file(tmp_path, bad):
    _touch(tmp_path, "001_ok.sql", bad)
    with pytest.raises(ValueError, match=bad):
        discover(str(tmp_path))


def test_duplicate_version_raises(tmp_path):
    _touch(tmp_path, "002_a.sql", "002_b.sql")
    with pytest.raises(ValueError, match="duplicate migration version 002"):
        discover(str(tmp_path))


def test_missing_directory_raises(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        discover(str(tmp_path / "nope"))


def test_empty_directory_returns_empty_list(tmp_path):
    assert discover(str(tmp_path)) == []
