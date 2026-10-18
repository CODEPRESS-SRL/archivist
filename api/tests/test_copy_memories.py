"""scripts.copy_memories against two real Postgres databases. [DOCKER]

Two migrated databases live inside the session container (``copy_src`` and
``copy_dst``): the copy tool is exercised exactly as at cutover (old -> new)
and as rollback (new -> old, ``--da-istante``). The DB-free parts (mapping
loader, refusal logic, diff planning, arguments) are in
``test_copy_memories_unit.py``.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncGenerator
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.auth import create_team
from app.models import Memory, MemoryFragment, MemoryHistory, Project, Team
from app.services import memory as svc
from app.services.hybrid import hybrid_search
from app.worker import embed_batch
from scripts import copy_memories as cm
from tests.fakes import FakeEmbedder

_API_DIR = Path(__file__).resolve().parent.parent
_TABLES = ("memory_history", "memory_fragments", "memories", "projects", "api_keys", "teams")
TEAM = "codepress"

MAPPA = cm.Mappa(
    team=TEAM,
    mappati=frozenset({"alfa", "beta"}),
    scartati=frozenset({"scarto"}),
)


# --- fixtures ---------------------------------------------------------------


def _url(base: str, database: str) -> str:
    return make_url(base).set(database=database).render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def url_coppia(postgres_url: str) -> tuple[str, str]:
    """Create and migrate two extra databases inside the session container."""

    async def _crea() -> None:
        admin = create_async_engine(postgres_url, isolation_level="AUTOCOMMIT")
        async with admin.connect() as conn:
            for nome in ("copy_src", "copy_dst"):
                await conn.execute(text(f"DROP DATABASE IF EXISTS {nome}"))
                await conn.execute(text(f"CREATE DATABASE {nome}"))
        await admin.dispose()

    asyncio.run(_crea())
    urls = (_url(postgres_url, "copy_src"), _url(postgres_url, "copy_dst"))
    for url in urls:
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=_API_DIR,
            env={**os.environ, "DATABASE_URL": url},
            check=True,
        )
    return urls


@pytest.fixture
async def motori(url_coppia: tuple[str, str]) -> AsyncGenerator[tuple[AsyncEngine, AsyncEngine]]:
    """(sorgente, destinazione) engines on clean tables, each with the team."""
    src, dst = (create_async_engine(u) for u in url_coppia)
    for motore in (src, dst):
        async with motore.begin() as conn:
            await conn.execute(text(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE"))
        async with _sess(motore)() as s:
            await create_team(s, TEAM)
    yield src, dst
    await src.dispose()
    await dst.dispose()


def _sess(motore: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(motore, expire_on_commit=False)


async def _team_id(motore: AsyncEngine) -> int:
    async with _sess(motore)() as s:
        return (await s.execute(select(Team.id).where(Team.name == TEAM))).scalar_one()


async def _now(motore: AsyncEngine) -> datetime:
    async with _sess(motore)() as s:
        return (await s.execute(select(func.clock_timestamp()))).scalar_one()


async def _seed(motore: AsyncEngine) -> None:
    """A realistic source: versions, fragments, a soft delete, a discarded slug."""
    tid = await _team_id(motore)
    async with _sess(motore)() as s:
        await svc.create_entry(s, tid, "alfa", "redis-cache", "redis caching notes", "alice")
        await svc.create_entry(s, tid, "alfa", "deploy-flow", "deploy pipeline notes", "bob")
        await svc.create_entry(s, tid, "alfa", "vecchia", "obsolete entry to delete", "alice")
        await svc.create_entry(s, tid, "beta", "auth-model", "token auth model", "carol")
        await svc.create_entry(s, tid, "scarto", "rumore", "benchmark noise", "alice")
        await svc.append_fragment(s, tid, "alfa", "redis-cache", "fragment uno", "bob")
        await svc.append_fragment(s, tid, "alfa", "redis-cache", "fragment due", "carol")
        await svc.append_fragment(s, tid, "beta", "auth-model", "fragment auth", "alice")
        await svc.compact_entry(
            s, tid, "alfa", "deploy-flow", "deploy pipeline notes v2", 1, None, "bob"
        )
        await svc.delete_entry(s, tid, "alfa", "vecchia", "dave")
    async with _sess(motore)() as s:
        await embed_batch(s, FakeEmbedder(), batch=100)
    # Created AFTER the embedding pass: stays embedding_stale=True, no vector.
    async with _sess(motore)() as s:
        await svc.create_entry(s, tid, "beta", "stale-entry", "not embedded yet", "erin")


def _vec(valore: object) -> list[float] | None:
    return None if valore is None else [float(x) for x in valore]  # type: ignore[attr-defined]


async def _istantanea(motore: AsyncEngine, slug_esclusi: set[str] | None = None) -> dict:
    """Everything that matters, keyed by natural keys, with no ids."""
    out: dict = {}
    async with _sess(motore)() as s:
        righe = (
            await s.execute(
                select(Project.slug, Memory)
                .join(Memory, Memory.project_id == Project.id)
                .order_by(Project.slug, Memory.keyword)
            )
        ).all()
        for slug, m in righe:
            if slug_esclusi and slug in slug_esclusi:
                continue
            frammenti = (
                await s.execute(select(MemoryFragment).where(MemoryFragment.memory_id == m.id))
            ).scalars()
            storico = (
                await s.execute(select(MemoryHistory).where(MemoryHistory.memory_id == m.id))
            ).scalars()
            out[(slug, m.keyword)] = {
                "content": m.content,
                "version": m.version,
                "token_count": m.token_count,
                "embedding": _vec(m.embedding),
                "embedding_stale": m.embedding_stale,
                "embedding_model": m.embedding_model,
                "keyword_embedding": _vec(m.keyword_embedding),
                "updated_by": m.updated_by,
                "deleted_at": m.deleted_at,
                "created_at": m.created_at,
                "updated_at": m.updated_at,
                "frammenti": sorted(
                    (f.created_at, f.content, f.created_by, f.token_count) for f in frammenti
                ),
                "storico": sorted(
                    (h.version, h.content, h.updated_by, h.created_at) for h in storico
                ),
            }
    return out


async def _conteggi_db(motore: AsyncEngine, slug: str) -> cm.Conteggi:
    async with _sess(motore)() as s:
        pid = (await s.execute(select(Project.id).where(Project.slug == slug))).scalar_one_or_none()
        if pid is None:
            return cm.Conteggi()
        mem = select(Memory.id).where(Memory.project_id == pid)
        return cm.Conteggi(
            attive=(
                await s.execute(
                    select(func.count()).where(
                        Memory.project_id == pid, Memory.deleted_at.is_(None)
                    )
                )
            ).scalar_one(),
            cancellate=(
                await s.execute(
                    select(func.count()).where(
                        Memory.project_id == pid, Memory.deleted_at.is_not(None)
                    )
                )
            ).scalar_one(),
            frammenti=(
                await s.execute(select(func.count()).where(MemoryFragment.memory_id.in_(mem)))
            ).scalar_one(),
            storico=(
                await s.execute(select(func.count()).where(MemoryHistory.memory_id.in_(mem)))
            ).scalar_one(),
        )


async def _conta(motore: AsyncEngine, tabella: type) -> int:
    async with _sess(motore)() as s:
        return (await s.execute(select(func.count()).select_from(tabella))).scalar_one()


def _scrivi_mappa(tmp_path: Path, mappa: cm.Mappa) -> Path:
    import json

    percorso = tmp_path / "mappa.json"
    percorso.write_text(
        json.dumps(
            {
                "team": mappa.team,
                "progetti": [{"slug": [{"slug": s} for s in sorted(mappa.mappati)]}],
                "scartati": [{"slug": s} for s in sorted(mappa.scartati)],
            }
        ),
        encoding="utf-8",
    )
    return percorso


# --- (a) refuses to start on an undecided slug ---------------------------------


async def test_refuses_to_start_when_source_has_an_undecided_slug(
    motori: tuple[AsyncEngine, AsyncEngine], url_coppia: tuple[str, str], tmp_path: Path
) -> None:
    src, dst = motori
    await _seed(src)
    tid = await _team_id(src)
    async with _sess(src)() as s:
        await svc.create_entry(s, tid, "orfano-uno", "k", "senza decisione", "alice")
        await svc.create_entry(s, tid, "orfano-due", "k", "senza decisione", "alice")

    with pytest.raises(cm.SlugSenzaDecisione) as info:
        await cm.copia(src, dst, MAPPA)
    assert info.value.slugs == ["orfano-due", "orfano-uno"]
    assert "orfano-uno" in str(info.value) and "orfano-due" in str(info.value)
    assert await _conta(dst, Project) == 0
    assert await _conta(dst, Memory) == 0

    # The CLI: exit != 0, message on stderr listing the slugs, destination untouched.
    risultato = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-m",
            "scripts.copy_memories",
            "--from",
            url_coppia[0],
            "--to",
            url_coppia[1],
            "--mappa",
            str(_scrivi_mappa(tmp_path, MAPPA)),
        ],
        cwd=_API_DIR,
        capture_output=True,
        text=True,
    )
    assert risultato.returncode != 0
    assert "orfano-uno" in risultato.stderr and "orfano-due" in risultato.stderr
    assert await _conta(dst, Memory) == 0


# --- (b) copies only the mapped slugs, with everything attached -----------------


async def test_copies_mapped_slugs_with_deleted_fragments_history_and_authors(
    motori: tuple[AsyncEngine, AsyncEngine],
) -> None:
    src, dst = motori
    await _seed(src)
    await cm.copia(src, dst, MAPPA)

    attesa = await _istantanea(src, slug_esclusi={"scarto"})
    trovata = await _istantanea(dst)
    assert trovata == attesa
    assert set(slug for slug, _ in trovata) == {"alfa", "beta"}  # "scarto" is absent

    # Spot checks on what the equality above already covers.
    vecchia = trovata[("alfa", "vecchia")]
    assert vecchia["deleted_at"] is not None and vecchia["updated_by"] == "dave"
    deploy = trovata[("alfa", "deploy-flow")]
    assert deploy["version"] == 2 and [h[0] for h in deploy["storico"]] == [1, 2]
    assert len(trovata[("alfa", "redis-cache")]["frammenti"]) == 2
    assert {f[2] for f in trovata[("alfa", "redis-cache")]["frammenti"]} == {"bob", "carol"}


# --- (c) embeddings as values; search is identical ------------------------------


async def test_embeddings_copied_as_is_and_search_is_identical(
    motori: tuple[AsyncEngine, AsyncEngine],
) -> None:
    src, dst = motori
    await _seed(src)
    await cm.copia(src, dst, MAPPA)

    a, b = await _istantanea(src), await _istantanea(dst)
    assert a[("alfa", "redis-cache")]["embedding"] is not None
    assert a[("beta", "stale-entry")]["embedding"] is None
    assert a[("beta", "stale-entry")]["embedding_stale"] is True
    for chiave in b:
        assert b[chiave]["embedding"] == a[chiave]["embedding"]
        assert b[chiave]["embedding_stale"] == a[chiave]["embedding_stale"]
        assert b[chiave]["embedding_model"] == a[chiave]["embedding_model"]
        assert b[chiave]["keyword_embedding"] == a[chiave]["keyword_embedding"]

    tid_src, tid_dst = await _team_id(src), await _team_id(dst)

    # Known behaviour change (N7): a discarded slug is not copied, so scope "all"
    # on the destination never reaches it, while the source still does. Pin it
    # here, then drop the slug from the source so the scores below (rank-based)
    # are compared between equivalent corpora.
    async with _sess(src)() as s1, _sess(dst)() as s2:
        tutti_src = await hybrid_search(s1, FakeEmbedder(), tid_src, "alfa", "notes", 5, "all")
        tutti_dst = await hybrid_search(s2, FakeEmbedder(), tid_dst, "alfa", "notes", 5, "all")
    assert "rumore" in {r["keyword"] for r in tutti_src["results"]}
    assert "rumore" not in {r["keyword"] for r in tutti_dst["results"]}
    async with _sess(src)() as s:
        await s.execute(
            text(
                "DELETE FROM memories WHERE project_id IN "
                "(SELECT id FROM projects WHERE slug = 'scarto')"
            )
        )
        await s.execute(text("DELETE FROM projects WHERE slug = 'scarto'"))
        await s.commit()

    interrogazioni = [
        ("alfa", "redis caching", "project"),
        ("alfa", "deploy", "project"),
        ("beta", "auth token", "project"),
        ("alfa", "notes", "all"),
        ("beta", "embedded", "all"),
    ]
    assert len(interrogazioni) == 5
    for progetto, query, scope in interrogazioni:
        async with _sess(src)() as s1, _sess(dst)() as s2:
            r1 = await hybrid_search(s1, FakeEmbedder(), tid_src, progetto, query, 5, scope)
            r2 = await hybrid_search(s2, FakeEmbedder(), tid_dst, progetto, query, 5, scope)
        assert [(r["keyword"], r["score"]) for r in r2["results"]] == [
            (r["keyword"], r["score"]) for r in r1["results"]
        ]
        assert [(r["keyword"], r["score"], r["excerpt"]) for r in r2["results"]] == [
            (r["keyword"], r["score"], r["excerpt"]) for r in r1["results"]
        ]
        assert [(r["keyword"], r["score"]) for r in r2["related_keywords"]] == [
            (r["keyword"], r["score"]) for r in r1["related_keywords"]
        ]


# --- (d) counts per slug --------------------------------------------------------


async def test_counts_per_slug_match_before_and_after(
    motori: tuple[AsyncEngine, AsyncEngine],
) -> None:
    src, dst = motori
    await _seed(src)
    prima = {slug: await _conteggi_db(src, slug) for slug in ("alfa", "beta")}
    rapporto = await cm.copia(src, dst, MAPPA)

    assert set(rapporto.per_slug) == {"alfa", "beta"}
    for slug in ("alfa", "beta"):
        assert rapporto.per_slug[slug] == prima[slug]
        assert await _conteggi_db(dst, slug) == prima[slug]
        assert rapporto.da_scrivere[slug] == prima[slug]  # destination was empty
    assert prima["alfa"].cancellate == 1 and prima["alfa"].frammenti == 2


async def test_solo_conteggi_writes_nothing_but_reports(
    motori: tuple[AsyncEngine, AsyncEngine],
) -> None:
    src, dst = motori
    await _seed(src)
    rapporto = await cm.copia(src, dst, MAPPA, solo_conteggi=True)
    assert rapporto.solo_conteggi is True
    assert rapporto.per_slug["alfa"].attive == 2 and rapporto.per_slug["alfa"].cancellate == 1
    assert rapporto.da_scrivere["alfa"].attive == 2
    assert await _conta(dst, Project) == 0
    assert await _conta(dst, Memory) == 0
    assert await _conta(dst, MemoryFragment) == 0
    assert await _conta(dst, MemoryHistory) == 0


# --- (e) idempotent ----------------------------------------------------------------


async def test_second_run_duplicates_nothing(motori: tuple[AsyncEngine, AsyncEngine]) -> None:
    src, dst = motori
    await _seed(src)
    await cm.copia(src, dst, MAPPA)
    dopo_prima = await _istantanea(dst)
    n = [await _conta(dst, t) for t in (Project, Memory, MemoryFragment, MemoryHistory)]

    secondo = await cm.copia(src, dst, MAPPA)
    assert await _istantanea(dst) == dopo_prima
    assert [await _conta(dst, t) for t in (Project, Memory, MemoryFragment, MemoryHistory)] == n
    for slug in ("alfa", "beta"):
        assert secondo.da_scrivere[slug] == cm.Conteggi()


# --- (f) reverse direction ------------------------------------------------------------


async def test_reverse_copy_with_da_istante_brings_back_only_new_writes(
    motori: tuple[AsyncEngine, AsyncEngine],
) -> None:
    vecchio, nuovo = motori
    await _seed(vecchio)
    await cm.copia(vecchio, nuovo, MAPPA)  # cutover
    istante = await _now(nuovo)
    await asyncio.sleep(0.05)

    tid = await _team_id(nuovo)
    async with _sess(nuovo)() as s:
        await svc.create_entry(s, tid, "alfa", "nuova-voce", "scritta dopo il cutover", "frank")
        await svc.append_fragment(s, tid, "alfa", "redis-cache", "fragment dopo", "frank")
        await svc.compact_entry(
            s, tid, "beta", "auth-model", "token auth model v2", 1, None, "gina"
        )
    prima_sul_vecchio = await _istantanea(vecchio)

    rapporto = await cm.copia(nuovo, vecchio, MAPPA, da_istante=istante)
    dopo = await _istantanea(vecchio)

    # New entry arrived, with its history.
    assert dopo[("alfa", "nuova-voce")]["updated_by"] == "frank"
    assert [h[0] for h in dopo[("alfa", "nuova-voce")]["storico"]] == [1]
    # A fragment alone on an existing entry arrived; the entry itself is untouched.
    redis_prima, redis_dopo = (
        prima_sul_vecchio[("alfa", "redis-cache")],
        dopo[("alfa", "redis-cache")],
    )
    assert len(redis_dopo["frammenti"]) == len(redis_prima["frammenti"]) + 1
    assert {k: v for k, v in redis_dopo.items() if k != "frammenti"} == {
        k: v for k, v in redis_prima.items() if k != "frammenti"
    }
    # Higher version on the source: updated, history kept.
    auth = dopo[("beta", "auth-model")]
    assert auth["version"] == 2 and auth["content"] == "token auth model v2"
    assert auth["updated_by"] == "gina"
    assert [h[0] for h in auth["storico"]] == [1, 2]
    # Everything else is exactly as it was.
    toccate = {("alfa", "nuova-voce"), ("alfa", "redis-cache"), ("beta", "auth-model")}
    for chiave, valore in prima_sul_vecchio.items():
        if chiave not in toccate:
            assert dopo[chiave] == valore
    # The window leaves out entries with nothing newer than the instant.
    assert rapporto.per_slug["alfa"].attive == 2  # nuova-voce and redis-cache
    assert rapporto.per_slug["alfa"].cancellate == 0

    # And a second reverse run changes nothing.
    await cm.copia(nuovo, vecchio, MAPPA, da_istante=istante)
    assert await _istantanea(vecchio) == dopo


# --- (g) team by name, never created -----------------------------------------------------


async def test_team_missing_on_destination_is_an_error_and_nothing_is_created(
    motori: tuple[AsyncEngine, AsyncEngine],
) -> None:
    src, dst = motori
    await _seed(src)
    async with _sess(dst)() as s:
        await s.execute(Team.__table__.delete())
        await s.commit()
    with pytest.raises(cm.ErroreCopia, match="destinazione"):
        await cm.copia(src, dst, MAPPA)
    assert await _conta(dst, Team) == 0
    assert await _conta(dst, Project) == 0


async def test_team_missing_on_source_is_an_error(
    motori: tuple[AsyncEngine, AsyncEngine],
) -> None:
    src, dst = motori
    async with _sess(src)() as s:
        await s.execute(Team.__table__.delete())
        await s.commit()
    with pytest.raises(cm.ErroreCopia, match="sorgente"):
        await cm.copia(src, dst, MAPPA)


async def test_team_is_resolved_by_name_not_by_id(
    motori: tuple[AsyncEngine, AsyncEngine],
) -> None:
    """Different ids on the two sides: the copy lands under the destination's id."""
    src, dst = motori
    async with _sess(dst)() as s:
        await create_team(s, "altro-team")  # shifts nothing; ids differ via identity gaps
        await s.execute(Team.__table__.delete().where(Team.name == TEAM))
        await create_team(s, TEAM)
    await _seed(src)
    assert await _team_id(src) != await _team_id(dst)
    await cm.copia(src, dst, MAPPA)
    tid_dst = await _team_id(dst)
    async with _sess(dst)() as s:
        team_dei_progetti = set(
            (await s.execute(select(Project.team_id).distinct())).scalars().all()
        )
    assert team_dei_progetti == {tid_dst}


# --- generations: delete + recreate after the cutover -------------------------------------


async def _righe(motore: AsyncEngine, slug: str, base: str) -> list[dict]:
    """Every generation of `base` in `slug`, oldest first, with its attachments."""
    async with _sess(motore)() as s:
        memorie = (
            (
                await s.execute(
                    select(Memory)
                    .join(Project, Project.id == Memory.project_id)
                    .where(
                        Project.slug == slug,
                        (Memory.keyword == base) | Memory.keyword.like(f"{base}#deleted#%"),
                    )
                    .order_by(Memory.created_at)
                )
            )
            .scalars()
            .all()
        )
        out = []
        for m in memorie:
            frammenti = (
                await s.execute(select(MemoryFragment).where(MemoryFragment.memory_id == m.id))
            ).scalars()
            storico = (
                await s.execute(select(MemoryHistory).where(MemoryHistory.memory_id == m.id))
            ).scalars()
            out.append(
                {
                    "keyword": m.keyword,
                    "content": m.content,
                    "version": m.version,
                    "deleted": m.deleted_at is not None,
                    "created_at": m.created_at,
                    "frammenti": sorted(f.content for f in frammenti),
                    "storico": sorted(h.version for h in storico),
                }
            )
        return out


async def test_recreated_entry_after_cutover_comes_back_as_its_own_generation(
    motori: tuple[AsyncEngine, AsyncEngine],
) -> None:
    vecchio, nuovo = motori
    await _seed(vecchio)  # "vecchia" is soft-deleted at v1 here
    await cm.copia(vecchio, nuovo, MAPPA)
    istante = await _now(nuovo)
    await asyncio.sleep(0.05)

    tid = await _team_id(nuovo)
    async with _sess(nuovo)() as s:
        # delete_entry would refuse (already deleted): recreate straight away,
        # the app renames the deleted row aside by itself.
        await svc.create_entry(s, tid, "alfa", "vecchia", "seconda generazione", "frank")
        await svc.append_fragment(s, tid, "alfa", "vecchia", "frammento nuovo", "frank")
        await svc.compact_entry(
            s, tid, "alfa", "vecchia", "seconda generazione v2", 1, None, "gina"
        )
    sul_nuovo = await _righe(nuovo, "alfa", "vecchia")
    assert [r["deleted"] for r in sul_nuovo] == [True, False]

    prima = (await _righe(vecchio, "alfa", "vecchia"))[0]
    await cm.copia(nuovo, vecchio, MAPPA, da_istante=istante)
    sul_vecchio = await _righe(vecchio, "alfa", "vecchia")

    assert len(sul_vecchio) == 2
    vecchia_gen, nuova_gen = sul_vecchio
    # The old generation row is untouched, still deleted, with its own history.
    assert vecchia_gen["deleted"] is True
    assert vecchia_gen["content"] == prima["content"] and vecchia_gen["storico"] == prima["storico"]
    assert vecchia_gen["frammenti"] == prima["frammenti"]
    assert vecchia_gen["keyword"].startswith("vecchia#deleted#")
    # The recreated entry is live on the old DB, with its own history and fragment.
    assert nuova_gen["keyword"] == "vecchia"
    assert nuova_gen["deleted"] is False
    assert nuova_gen["content"] == "seconda generazione v2" and nuova_gen["version"] == 2
    assert nuova_gen["storico"] == [1, 2]
    assert nuova_gen["frammenti"] == ["frammento nuovo"]
    assert nuova_gen["created_at"] == sul_nuovo[1]["created_at"]

    # A second reverse run plans nothing.
    dopo = await _istantanea(vecchio)
    secondo = await cm.copia(nuovo, vecchio, MAPPA, da_istante=istante)
    assert await _istantanea(vecchio) == dopo
    assert secondo.da_scrivere["alfa"] == cm.Conteggi()


async def test_forward_copy_renames_a_different_generation_aside_like_the_app(
    motori: tuple[AsyncEngine, AsyncEngine],
) -> None:
    """Same (project, keyword), different created_at on the two sides."""
    vecchio, nuovo = motori
    tid_v, tid_n = await _team_id(vecchio), await _team_id(nuovo)
    async with _sess(nuovo)() as s:
        await svc.create_entry(s, tid_n, "alfa", "k", "generazione sul nuovo", "ann")
    await asyncio.sleep(0.05)
    async with _sess(vecchio)() as s:
        await svc.create_entry(s, tid_v, "alfa", "k", "generazione sul vecchio", "bob")

    await cm.copia(vecchio, nuovo, MAPPA)
    righe = await _righe(nuovo, "alfa", "k")
    assert len(righe) == 2
    messa_da_parte, copiata = righe
    assert messa_da_parte["content"] == "generazione sul nuovo"
    assert messa_da_parte["keyword"].startswith("k#deleted#") and messa_da_parte["deleted"]
    assert copiata["keyword"] == "k" and copiata["deleted"] is False
    assert copiata["content"] == "generazione sul vecchio"

    dopo = await _istantanea(nuovo)
    await cm.copia(vecchio, nuovo, MAPPA)
    assert await _istantanea(nuovo) == dopo


async def test_renamed_source_row_never_reuses_the_source_suffix(
    motori: tuple[AsyncEngine, AsyncEngine],
) -> None:
    vecchio, nuovo = motori
    tid = await _team_id(vecchio)
    async with _sess(vecchio)() as s:
        await svc.create_entry(s, tid, "alfa", "k", "prima", "ann")
        await svc.delete_entry(s, tid, "alfa", "k", "ann")
        await svc.create_entry(s, tid, "alfa", "k", "seconda", "ann")
    # Occupy ids on the destination so that suffixes would differ.
    tid_n = await _team_id(nuovo)
    async with _sess(nuovo)() as s:
        for i in range(3):
            await svc.create_entry(s, tid_n, "alfa", f"zz-{i}", "riempitivo", "ann")
    async with _sess(vecchio)() as s:
        suffisso_sorgente = (
            await s.execute(select(Memory.keyword).where(Memory.keyword.like("k#deleted#%")))
        ).scalar_one()

    await cm.copia(vecchio, nuovo, MAPPA)
    async with _sess(nuovo)() as s:
        riga = (
            await s.execute(select(Memory).where(Memory.keyword.like("k#deleted#%")))
        ).scalar_one()
    assert riga.keyword == f"k#deleted#{riga.id}"
    assert riga.keyword != suffisso_sorgente
    assert [r["deleted"] for r in await _righe(nuovo, "alfa", "k")] == [True, False]
