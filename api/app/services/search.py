"""Exact + FTS + related-keyword search over memories and fragments.

``search_memories`` runs a single ranked query (one CTE, three score tiers)
plus a separate related-keyword suggestion query. Pure SQL, zero LLM.

Score tiers, highest wins per memory (dedup by ``memory_id``):

1. **Keyword** — exact match ``= normalize(query)`` → 3.0; prefix
   ``LIKE normalize(query) || '%'`` → 2.5; otherwise trigram
   ``similarity(keyword, normalize(query)) > 0.35`` → ``2.0 * similarity``.
2. **FTS over content ∪ fragments** — ``plainto_tsquery('simple', query)``
   against the entry's ``content_tsv`` and its fragments'; per memory the max
   ``ts_rank_cd(tsv, query, 32)`` (flag 32 = rank/(rank+1) + length dampening,
   the cheap stand-in for BM25) → ``1.0 * rank``.

The keyword tier stays index-friendly: ``LIKE 'prefix%'`` and the ``%`` trigram
operator both use the ``gin_trgm_ops`` index (``%`` at the default 0.3 threshold
is a superset that the explicit ``> 0.35`` check refines), and the FTS tiers hit
the ``content_tsv`` GIN indexes — so the ranked query never sequentially scans
``memories`` at scale.

``scope`` is ``"project"`` (filter to one project) or ``"all"`` (cross-project);
the ``project`` field is always present in every result and related item.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Project
from app.utils.keywords import normalize_keyword

# Excerpt: ~40 words around the match (non-fragment mode, so a keyword-only hit
# with no FTS match still yields the leading words of the content).
_HEADLINE_OPTS = "MaxWords=40, MinWords=15"

# Ranked search: keyword tier + content FTS + fragment FTS, unioned then reduced
# to one row per memory by the max score. ``{scope}`` is a fixed, non-user filter
# fragment injected below (scope is validated to project|all first).
_SEARCH_SQL = """
WITH candidates AS (
    -- Tier 1: keyword exact / prefix / trigram (index-friendly WHERE)
    SELECT
        m.id AS memory_id,
        CASE
            WHEN m.keyword = :nkw THEN 3.0
            WHEN m.keyword LIKE :nkw || '%' THEN 2.5
            ELSE 2.0 * similarity(m.keyword, :nkw)
        END AS score
    FROM memories m
    WHERE m.deleted_at IS NULL
      AND :nkw <> ''
      {scope}
      AND (
          m.keyword LIKE :nkw || '%'
          OR (m.keyword % :nkw AND similarity(m.keyword, :nkw) > 0.35)
      )

    UNION ALL

    -- Tier 2a: FTS over the entry content
    SELECT
        m.id AS memory_id,
        1.0 * ts_rank_cd(m.content_tsv, plainto_tsquery('simple', :query), 32) AS score
    FROM memories m
    WHERE m.deleted_at IS NULL
      {scope}
      AND m.content_tsv @@ plainto_tsquery('simple', :query)

    UNION ALL

    -- Tier 2b: FTS over the entry fragments
    SELECT
        f.memory_id AS memory_id,
        1.0 * ts_rank_cd(f.content_tsv, plainto_tsquery('simple', :query), 32) AS score
    FROM memory_fragments f
    JOIN memories m ON m.id = f.memory_id
    WHERE m.deleted_at IS NULL
      {scope}
      AND f.content_tsv @@ plainto_tsquery('simple', :query)
),
ranked AS (
    SELECT memory_id, max(score) AS score
    FROM candidates
    GROUP BY memory_id
)
SELECT
    p.slug AS project,
    m.keyword AS keyword,
    ranked.score AS score,
    ts_headline(
        'simple',
        CASE
            WHEN m.content_tsv @@ plainto_tsquery('simple', :query) THEN m.content
            ELSE COALESCE(
                (
                    SELECT f.content
                    FROM memory_fragments f
                    WHERE f.memory_id = m.id
                      AND f.content_tsv @@ plainto_tsquery('simple', :query)
                    ORDER BY ts_rank_cd(f.content_tsv, plainto_tsquery('simple', :query), 32) DESC
                    LIMIT 1
                ),
                m.content
            )
        END,
        plainto_tsquery('simple', :query),
        '{headline_opts}'
    ) AS excerpt,
    (
        SELECT count(*) FROM memory_fragments mf WHERE mf.memory_id = m.id
    ) AS pending_fragments,
    m.token_count AS token_count,
    m.updated_at AS updated_at
FROM ranked
JOIN memories m ON m.id = ranked.memory_id
JOIN projects p ON p.id = m.project_id
ORDER BY ranked.score DESC, m.keyword
LIMIT :limit
"""

# Related keywords: trigram neighbours of the query, excluding the hits already
# returned above. Not on the hot path, so a plain ``similarity > 0.2`` filter is
# fine (the 0.2 threshold is below pg_trgm's default ``%`` cutoff, so ``%`` would
# wrongly exclude the 0.2–0.3 band).
_RELATED_SQL = """
SELECT p.slug AS project, m.keyword AS keyword
FROM memories m
JOIN projects p ON p.id = m.project_id
WHERE m.deleted_at IS NULL
  AND :nkw <> ''
  {scope}
  AND similarity(m.keyword, :nkw) > 0.2
ORDER BY similarity(m.keyword, :nkw) DESC, m.keyword
LIMIT :related_limit
"""


def _strip_highlight(excerpt: str | None) -> str:
    """Drop ts_headline's default ``<b>`` markers, leaving plain text."""
    if not excerpt:
        return ""
    return excerpt.replace("<b>", "").replace("</b>", "")


async def search_memories(
    session: AsyncSession,
    team_id: int,
    project: str,
    query: str,
    limit: int = 5,
    scope: str = "project",
) -> dict:
    """Search memories by keyword + full text; suggest related keywords.

    ``scope="project"`` restricts to the given project (resolved by slug,
    within ``team_id``); ``scope="all"`` searches every project of THIS team
    — never another team's. Every result and related item carries its own
    ``project`` slug (single contract). A missing project or a query with no
    matches yields ``[]`` results — never an error.
    """
    if scope not in ("project", "all"):
        raise ValueError(f"scope must be 'project' or 'all', got {scope!r}")

    # normalize(query) drives the keyword tier; an empty/symbol-only query
    # normalizes to nothing → the `:nkw <> ''` guards make it match no keyword.
    try:
        nkw = normalize_keyword(query)
    except ValueError:
        nkw = ""

    params: dict = {"nkw": nkw, "query": query, "limit": limit, "team_id": team_id}
    if scope == "project":
        project_slug = normalize_keyword(project)
        project_id = (
            await session.execute(
                select(Project.id).where(Project.team_id == team_id, Project.slug == project_slug)
            )
        ).scalar_one_or_none()
        if project_id is None:
            return {"results": [], "related_keywords": []}
        scope_filter = "AND m.project_id = :project_id"
        params["project_id"] = project_id
    else:
        # scope == "all": every project of THIS team, never another team's.
        scope_filter = "AND m.project_id IN (SELECT id FROM projects WHERE team_id = :team_id)"

    search_sql = text(_SEARCH_SQL.format(scope=scope_filter, headline_opts=_HEADLINE_OPTS))
    rows = (await session.execute(search_sql, params)).mappings().all()
    results = [
        {
            "project": r["project"],
            "keyword": r["keyword"],
            "score": float(r["score"]),
            "excerpt": _strip_highlight(r["excerpt"]),
            "pending_fragments": r["pending_fragments"],
            "token_count": r["token_count"],
            "age_days": (datetime.now(timezone.utc) - r["updated_at"]).days,
        }
        for r in rows
    ]

    # Fetch a few extra related candidates so excluding the results still leaves
    # room for up to `search_limit_default` suggestions.
    seen = {(r["project"], r["keyword"]) for r in results}
    related_params = dict(params)
    related_params["related_limit"] = get_settings().search_limit_default + len(results)
    related_sql = text(_RELATED_SQL.format(scope=scope_filter))
    related_rows = (await session.execute(related_sql, related_params)).mappings().all()
    related_keywords: list[dict] = []
    for r in related_rows:
        pair = (r["project"], r["keyword"])
        if pair in seen:
            continue
        related_keywords.append({"project": r["project"], "keyword": r["keyword"]})
        if len(related_keywords) >= get_settings().search_limit_default:
            break

    return {"results": results, "related_keywords": related_keywords}
