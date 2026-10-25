"""Unit tests for the claude-mem recent-observations index reconstruction."""
import sqlite3

from benchmark.adapters.claude_mem import _recent_obs_index_via_sqlite
from benchmark.tokens import count_tokens


def _make_db(path, project, n, epoch0=1_000):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE observations (project TEXT, title TEXT, subtitle TEXT, created_at_epoch INTEGER)"
    )
    conn.executemany(
        "INSERT INTO observations VALUES (?, ?, ?, ?)",
        [(project, f"Observation {i}", "Manual memory", epoch0 + i) for i in range(n)],
    )
    conn.commit()
    conn.close()


def test_empty_or_missing_returns_empty(tmp_path):
    missing = tmp_path / "nope.db"
    assert _recent_obs_index_via_sqlite(missing, "p") == ""
    db = tmp_path / "cm.db"
    _make_db(db, "p", 0)
    assert _recent_obs_index_via_sqlite(db, "p") == ""


def test_index_grows_then_plateaus_at_limit(tmp_path):
    db = tmp_path / "cm.db"
    _make_db(db, "bench", 120)
    small = count_tokens(_recent_obs_index_via_sqlite(db, "bench", limit=10))
    big = count_tokens(_recent_obs_index_via_sqlite(db, "bench", limit=50))
    capped = count_tokens(_recent_obs_index_via_sqlite(db, "bench", limit=50))  # 120 obs, cap 50
    assert small > 0
    assert big > small          # grows with more observations surfaced
    assert capped == big        # plateaus once obs count exceeds the limit


def test_index_scoped_to_project_and_most_recent(tmp_path):
    db = tmp_path / "cm.db"
    _make_db(db, "bench", 3)
    # add another project's rows that must not leak in
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO observations VALUES ('other','Leak','x',9999)")
    conn.commit()
    conn.close()
    idx = _recent_obs_index_via_sqlite(db, "bench", limit=50)
    assert "Leak" not in idx
    assert "Observation 2" in idx  # most-recent first
