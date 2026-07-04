"""Integration tests for the search service (T1.4).

Exercised against a real Postgres (testcontainers, ``db_engine`` fixture) so the
FTS/trigram operators and GIN indexes behave exactly as in production. Covers the
six DoD checks: three-tier ranking, fragment recall, related keywords,
cross-project scope, the empty/matchless query, and the EXPLAIN-uses-GIN check.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app import db as app_db
from app.auth import create_team
from app.services.memory import append_fragment, create_entry
from app.services.search import search_memories

OWNER = "enrico"
PROJECT = "archivist"
OTHER_PROJECT = "billing-api"


@pytest.fixture
async def team_id(db_engine: object) -> int:
    async with app_db.SessionFactory() as session:
        return await create_team(session, "acme")


# --- helpers ---------------------------------------------------------------


async def _create(team_id: int, keyword: str, content: str, project: str = PROJECT) -> None:
    async with app_db.SessionFactory() as s:
        await create_entry(s, team_id, project, keyword, content, OWNER)


async def _append(team_id: int, keyword: str, fragment: str, project: str = PROJECT) -> None:
    async with app_db.SessionFactory() as s:
        await append_fragment(s, team_id, project, keyword, fragment, OWNER)


async def _search(
    team_id: int, query: str, *, limit: int = 5, scope: str = "project", project: str = PROJECT
):
    async with app_db.SessionFactory() as s:
        return await search_memories(s, team_id, project, query, limit=limit, scope=scope)


def _by_keyword(results: list[dict]) -> dict[str, dict]:
    return {r["keyword"]: r for r in results}


# --- ranking (DoD 1) -------------------------------------------------------


async def test_ranking_exact_beats_trigram_beats_fts(team_id: int) -> None:
    # Exact keyword hit, a trigram-only neighbour (not a prefix), and an entry
    # that only matches through full text — scores must strictly descend.
    await _create(team_id, "redis-cache", "an entry about caching")
    await _create(team_id, "cache-redis", "reverse worded neighbour")  # trigram, not prefix
    await _create(team_id, "session-store", "this note mentions redis-cache in its body")  # FTS only

    result = await _search(team_id, "redis-cache")
    by_kw = _by_keyword(result["results"])

    assert "redis-cache" in by_kw
    assert by_kw["redis-cache"]["score"] == 3.0  # exact
    # trigram neighbour present and below the exact score
    assert "cache-redis" in by_kw
    assert by_kw["cache-redis"]["score"] < 3.0
    # FTS-only entry present and below the trigram neighbour
    assert "session-store" in by_kw
    assert by_kw["session-store"]["score"] < by_kw["cache-redis"]["score"]
    # results are ordered by descending score
    scores = [r["score"] for r in result["results"]]
    assert scores == sorted(scores, reverse=True)
    assert result["results"][0]["keyword"] == "redis-cache"


async def test_prefix_scores_above_trigram(team_id: int) -> None:
    await _create(team_id, "redis-cache", "x")
    result = await _search(team_id, "redis")  # prefix of redis-cache
    assert _by_keyword(result["results"])["redis-cache"]["score"] == 2.5


# --- fragment recall (DoD 2) -----------------------------------------------


async def test_fragment_recall(team_id: int) -> None:
    # A term present ONLY in a fragment must still surface the parent entry.
    await _create(team_id, "deploy-notes", "general deployment information")
    await _append(team_id, "deploy-notes", "the pineapple rollout happened at midnight")

    result = await _search(team_id, "pineapple")
    by_kw = _by_keyword(result["results"])
    assert "deploy-notes" in by_kw
    # the excerpt is drawn from the matching fragment text
    assert "pineapple" in by_kw["deploy-notes"]["excerpt"].lower()


# --- related keywords (DoD 3) ----------------------------------------------


async def test_related_keywords_returns_trigram_neighbours(team_id: int) -> None:
    # Neighbours similar to the query but NOT returned as results appear under
    # related_keywords; a result keyword is excluded from related.
    await _create(team_id, "redis-cache", "caching")
    await _create(team_id, "redis-eviction", "eviction policy")

    result = await _search(team_id, "edis")  # fuzzy: 0.2 < similarity < 0.35, not prefix/FTS
    related = {r["keyword"] for r in result["related_keywords"]}
    result_kws = {r["keyword"] for r in result["results"]}

    assert "redis-cache" in related
    assert "redis-eviction" in related
    assert related.isdisjoint(result_kws)


async def test_related_excludes_result_keywords(team_id: int) -> None:
    await _create(team_id, "redis-cache", "caching")
    result = await _search(team_id, "redis-cache")  # exact result
    assert any(r["keyword"] == "redis-cache" for r in result["results"])
    assert all(r["keyword"] != "redis-cache" for r in result["related_keywords"])


# --- cross-project scope (DoD 4) -------------------------------------------


async def test_scope_project_isolates_current_project(team_id: int) -> None:
    await _create(team_id, "redis-cache", "in archivist", project=PROJECT)
    await _create(team_id, "redis-cache", "in billing", project=OTHER_PROJECT)

    result = await _search(team_id, "redis-cache", scope="project", project=PROJECT)
    assert [r["project"] for r in result["results"]] == [PROJECT]
    # the project field is always present even under scope="project"
    assert result["results"][0]["project"] == PROJECT


async def test_scope_all_searches_every_project(team_id: int) -> None:
    await _create(team_id, "redis-cache", "in archivist", project=PROJECT)
    await _create(team_id, "redis-cache", "in billing", project=OTHER_PROJECT)

    result = await _search(team_id, "redis-cache", scope="all", project=PROJECT)
    projects = sorted(r["project"] for r in result["results"])
    assert projects == sorted([PROJECT, OTHER_PROJECT])


# --- empty / matchless query (DoD 5) ---------------------------------------


async def test_empty_query_returns_empty_results(team_id: int) -> None:
    await _create(team_id, "redis-cache", "caching")
    result = await _search(team_id, "")  # normalizes to nothing → no error
    assert result["results"] == []
    assert result["related_keywords"] == []


async def test_symbols_only_query_returns_empty_results(team_id: int) -> None:
    await _create(team_id, "redis-cache", "caching")
    result = await _search(team_id, "!!!@@@")  # normalizes to empty keyword
    assert result["results"] == []


async def test_matchless_query_returns_empty_never_errors(team_id: int) -> None:
    await _create(team_id, "redis-cache", "caching")
    result = await _search(team_id, "xylophone-quokka-zephyr")
    assert result["results"] == []


async def test_missing_project_returns_empty(team_id: int) -> None:
    result = await _search(team_id, "redis-cache", scope="project", project="nonexistent-project")
    assert result == {"results": [], "related_keywords": []}


async def test_invalid_scope_raises(team_id: int) -> None:
    with pytest.raises(ValueError, match="scope"):
        await _search(team_id, "redis", scope="galaxy")


# --- output contract -------------------------------------------------------


async def test_result_shape_and_limit(team_id: int) -> None:
    for i in range(4):
        await _create(team_id, f"redis-cache-{i}", "caching note")
    result = await _search(team_id, "redis", limit=2)
    assert len(result["results"]) == 2
    row = result["results"][0]
    assert set(row) == {
        "project",
        "keyword",
        "score",
        "excerpt",
        "pending_fragments",
        "token_count",
        "age_days",
    }
    assert isinstance(row["score"], float)
    assert isinstance(row["pending_fragments"], int)


# --- EXPLAIN uses GIN (DoD 6) ----------------------------------------------


async def test_fts_query_uses_gin_index_no_seq_scan(team_id: int) -> None:
    # DoD: the FTS query rides the GIN indexes (no seq scan) at 1000 rows. The
    # rows are wide (many heap pages → seq scan is expensive) but share a tiny
    # vocabulary (small GIN → cheap index scan), so the planner naturally prefers
    # the GIN indexes — the realistic shape of dense memory content at scale.
    # (The keyword/trigram tier is a separate concern and legitimately scans a
    # small set of short keywords; the DoD scopes this check to the FTS query.)
    filler = "lorem ipsum dolor sit amet consectetur adipiscing elit "
    async with app_db.SessionFactory() as s:
        pid = (
            await s.execute(
                text("INSERT INTO projects (team_id, slug) VALUES (:t, :s) RETURNING id"),
                {"t": team_id, "s": PROJECT},
            )
        ).scalar_one()
        await s.execute(
            text(
                "INSERT INTO memories (project_id, keyword, content, token_count, updated_by) "
                "SELECT :pid, 'kw-' || g, repeat(:filler, 60), 200, :o "
                "FROM generate_series(1, 1000) AS g"
            ),
            {"pid": pid, "o": OWNER, "filler": filler},
        )
        # One entry (+ one fragment) carrying the rare needle term.
        mid = (
            await s.execute(
                text(
                    "INSERT INTO memories (project_id, keyword, content, token_count, updated_by) "
                    "VALUES (:pid, 'needle-entry', 'a uniquerarewordxyz appears here', 5, :o) "
                    "RETURNING id"
                ),
                {"pid": pid, "o": OWNER},
            )
        ).scalar_one()
        # Wide/low-vocab fragments so the fragment FTS GIN also wins at scale.
        await s.execute(
            text(
                "INSERT INTO memory_fragments (memory_id, content, token_count, created_by) "
                "SELECT :mid, repeat(:filler, 60), 200, :o FROM generate_series(1, 1000) AS g"
            ),
            {"mid": mid, "o": OWNER, "filler": filler},
        )
        await s.execute(
            text(
                "INSERT INTO memory_fragments (memory_id, content, token_count, created_by) "
                "VALUES (:mid, 'a fragmentonlyword lives here', 5, :o)"
            ),
            {"mid": mid, "o": OWNER},
        )
        await s.commit()

    async with app_db.SessionFactory() as s:
        await s.execute(text("ANALYZE memories"))
        await s.execute(text("ANALYZE memory_fragments"))

        # Tier 2a — FTS over entry content (exactly as search_memories issues it).
        content_fts = (
            "SELECT m.id FROM memories m "
            "WHERE m.deleted_at IS NULL AND m.project_id = :pid "
            "AND m.content_tsv @@ plainto_tsquery('simple', :q)"
        )
        content_plan = "\n".join(
            (
                await s.execute(
                    text("EXPLAIN " + content_fts), {"pid": pid, "q": "uniquerarewordxyz"}
                )
            )
            .scalars()
            .all()
        )
        assert "Seq Scan on memories" not in content_plan, content_plan
        assert "idx_memories_tsv" in content_plan, content_plan

        # Tier 2b — FTS over fragments.
        frag_fts = (
            "SELECT f.memory_id FROM memory_fragments f "
            "WHERE f.content_tsv @@ plainto_tsquery('simple', :q)"
        )
        frag_plan = "\n".join(
            (await s.execute(text("EXPLAIN " + frag_fts), {"q": "fragmentonlyword"}))
            .scalars()
            .all()
        )
        assert "Seq Scan on memory_fragments" not in frag_plan, frag_plan
        assert "idx_fragments_tsv" in frag_plan, frag_plan


# --- age_days ---------------------------------------------------------------


async def test_search_hits_carry_age_days(team_id: int) -> None:
    await _create(team_id, "redis-cache", "caching notes")
    out = await _search(team_id, "redis-cache")
    assert out["results"][0]["age_days"] == 0
