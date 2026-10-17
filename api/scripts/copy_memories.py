#!/usr/bin/env python
"""One-shot copy of Archivist memories between two databases, by NATURAL keys.

    python -m scripts.copy_memories --from <DATABASE_URL> --to <DATABASE_URL> \
        --mappa <file.json> [--da-istante <ISO>] [--solo-conteggi]

Used at cutover (old DB -> new DB) and, reversed, for rollback (new -> old,
only entries written after ``--da-istante``). Ids are NOT preserved: rows are
matched by

    project  = (team name -> team id, slug)
    memory   = (project, keyword)
    history  = (memory, version)
    fragment = (memory, created_at, content)

Embeddings and ``embedding_stale`` are copied as values. The team is resolved
by NAME on both sides and never created. The mapping file is the Workspace's
``deploy/memoria/mappatura.json`` (fields used: ``team``, ``progetti[].slug[].slug``,
``scartati[].slug``). Every project slug of the source team must be either
mapped or discarded: otherwise the tool refuses to start and writes nothing.

The whole run is ONE transaction on the destination; ``--solo-conteggi``
computes the report and writes nothing. The copy is additive: it inserts what
is missing and updates an entry only when the source has a higher version (or
soft-deleted it at the same version); it never touches anything else.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, exists, or_, select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.models import Memory, MemoryFragment, MemoryHistory, Project, Team  # noqa: E402

# --- errors -----------------------------------------------------------------


class ErroreCopia(Exception):
    """A refusal to run: bad mapping, missing team, unresolved slug."""


class SlugSenzaDecisione(ErroreCopia):
    """The source has project slugs that are neither mapped nor discarded."""

    def __init__(self, slugs: Sequence[str]) -> None:
        self.slugs = list(slugs)
        super().__init__(
            "la sorgente ha slug senza decisione (ne' mappati ne' scartati): "
            + ", ".join(self.slugs)
        )


# --- mapping ----------------------------------------------------------------


@dataclass(frozen=True)
class Mappa:
    team: str
    mappati: frozenset[str]
    scartati: frozenset[str]


def mappa_da_dati(dati: Any) -> Mappa:
    """Build a ``Mappa`` from the parsed JSON of ``mappatura.json``."""
    if not isinstance(dati, dict):
        raise ErroreCopia("mappatura: la radice deve essere un oggetto")
    team = dati.get("team")
    if not isinstance(team, str) or not team.strip():
        raise ErroreCopia("mappatura: 'team' mancante o non e' una stringa")

    def _slugs(chiave: str, voci: Any) -> list[str]:
        if not isinstance(voci, list):
            raise ErroreCopia(f"mappatura: '{chiave}' deve essere una lista")
        out: list[str] = []
        for voce in voci:
            slug = voce.get("slug") if isinstance(voce, dict) else None
            if not isinstance(slug, str) or not slug:
                raise ErroreCopia(f"mappatura: voce senza 'slug' in '{chiave}'")
            out.append(slug)
        return out

    progetti = dati.get("progetti")
    if not isinstance(progetti, list):
        raise ErroreCopia("mappatura: 'progetti' deve essere una lista")
    mappati: list[str] = []
    for progetto in progetti:
        if not isinstance(progetto, dict):
            raise ErroreCopia("mappatura: ogni progetto deve essere un oggetto")
        mappati += _slugs("progetti[].slug", progetto.get("slug"))
    scartati = _slugs("scartati", dati.get("scartati", []))

    sovrapposti = sorted(set(mappati) & set(scartati))
    if sovrapposti:
        raise ErroreCopia("mappatura: slug sia mappati sia scartati: " + ", ".join(sovrapposti))
    return Mappa(team=team, mappati=frozenset(mappati), scartati=frozenset(scartati))


def carica_mappa(percorso: str | Path) -> Mappa:
    try:
        dati = json.loads(Path(percorso).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ErroreCopia(f"mappatura illeggibile: {percorso}") from exc
    return mappa_da_dati(dati)


def slug_senza_decisione(slug_sorgente: Iterable[str], mappa: Mappa) -> list[str]:
    """Source slugs that are neither mapped nor discarded, sorted."""
    return sorted(set(slug_sorgente) - mappa.mappati - mappa.scartati)


def verifica_decisioni(slug_sorgente: Iterable[str], mappa: Mappa) -> None:
    """Refuse (raise) if the source has a slug without a decision."""
    mancanti = slug_senza_decisione(slug_sorgente, mappa)
    if mancanti:
        raise SlugSenzaDecisione(mancanti)


# --- report -----------------------------------------------------------------


@dataclass
class Conteggi:
    attive: int = 0
    cancellate: int = 0
    frammenti: int = 0
    storico: int = 0

    def somma(self, altro: Conteggi) -> Conteggi:
        return Conteggi(
            self.attive + altro.attive,
            self.cancellate + altro.cancellate,
            self.frammenti + altro.frammenti,
            self.storico + altro.storico,
        )


@dataclass
class Rapporto:
    """``per_slug``: what the source holds in scope (all of it, or the window
    after ``--da-istante``). ``da_scrivere``: what the destination was missing
    (written, or only counted with ``--solo-conteggi``)."""

    per_slug: dict[str, Conteggi] = field(default_factory=dict)
    da_scrivere: dict[str, Conteggi] = field(default_factory=dict)
    solo_conteggi: bool = False

    def totale(self) -> Conteggi:
        tot = Conteggi()
        for c in self.per_slug.values():
            tot = tot.somma(c)
        return tot


def formatta_rapporto(rapporto: Rapporto) -> str:
    righe = ["slug: attive cancellate frammenti storico (in sorgente | da scrivere)"]
    for slug in sorted(rapporto.per_slug):
        s = rapporto.per_slug[slug]
        d = rapporto.da_scrivere.get(slug, Conteggi())
        righe.append(
            f"{slug}: {s.attive} {s.cancellate} {s.frammenti} {s.storico}"
            f" | {d.attive} {d.cancellate} {d.frammenti} {d.storico}"
        )
    t = rapporto.totale()
    righe.append(f"totale: {t.attive} {t.cancellate} {t.frammenti} {t.storico}")
    if rapporto.solo_conteggi:
        righe.append("solo conteggi: nessuna scrittura")
    return "\n".join(righe)


# --- natural-key diff (pure) --------------------------------------------------


class Azione(Enum):
    NULLA = "nulla"
    INSERISCI = "inserisci"
    AGGIORNA = "aggiorna"


@dataclass(frozen=True)
class StatoMemoria:
    version: int
    cancellata: bool


def decidi_memoria(sorgente: StatoMemoria, destinazione: StatoMemoria | None) -> Azione:
    """What to do with one entry, matched by (project, keyword).

    Absent on the destination -> insert. Higher version on the source -> update.
    Same version but soft-deleted only on the source (a delete does not bump
    the version) -> update. Anything else -> leave it alone.
    """
    if destinazione is None:
        return Azione.INSERISCI
    if sorgente.version > destinazione.version:
        return Azione.AGGIORNA
    if (
        sorgente.version == destinazione.version
        and sorgente.cancellata
        and not destinazione.cancellata
    ):
        return Azione.AGGIORNA
    return Azione.NULLA


def chiave_frammento(created_at: datetime, content: str) -> tuple[datetime, str]:
    return (created_at, content)


def mancanti[T](sorgente: Iterable[T], presenti: set[T]) -> list[T]:
    """Source keys absent on the destination, in source order, deduplicated."""
    visti: set[T] = set()
    out: list[T] = []
    for chiave in sorgente:
        if chiave not in presenti and chiave not in visti:
            visti.add(chiave)
            out.append(chiave)
    return out


# --- CLI arguments (pure) -------------------------------------------------------


def url_async(url: str) -> str:
    """Accept ``postgresql://`` and ``postgres://`` URLs; asyncpg is the driver."""
    for prefisso in ("postgresql://", "postgres://"):
        if url.startswith(prefisso):
            return "postgresql+asyncpg://" + url[len(prefisso) :]
    return url


def parse_istante(valore: str) -> datetime:
    """ISO 8601 -> aware datetime (a naive value is read as UTC)."""
    try:
        dt = datetime.fromisoformat(valore)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"istante ISO non valido: {valore!r}") from exc
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def costruisci_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.copy_memories",
        description="Copy Archivist memories between two databases by natural keys.",
    )
    parser.add_argument("--from", dest="sorgente", required=True, metavar="DATABASE_URL")
    parser.add_argument("--to", dest="destinazione", required=True, metavar="DATABASE_URL")
    parser.add_argument("--mappa", required=True, metavar="FILE.json")
    parser.add_argument("--da-istante", dest="da_istante", type=parse_istante, default=None)
    parser.add_argument("--solo-conteggi", dest="solo_conteggi", action="store_true")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return costruisci_parser().parse_args(argv)


# --- database side ------------------------------------------------------------------

_COLONNE_MEMORIA = (
    "content",
    "version",
    "token_count",
    "embedding",
    "embedding_stale",
    "embedding_model",
    "keyword_embedding",
    "updated_by",
    "deleted_at",
    "created_at",
    "updated_at",
)
# The columns an update rewrites: everything but created_at (immutable).
_COLONNE_AGGIORNAMENTO = tuple(c for c in _COLONNE_MEMORIA if c != "created_at")


async def _team_id(session: AsyncSession, nome: str, lato: str) -> int:
    team_id = (await session.execute(select(Team.id).where(Team.name == nome))).scalar_one_or_none()
    if team_id is None:
        raise ErroreCopia(f"team {nome!r} inesistente sulla {lato} (non viene creato)")
    return team_id


def _filtro_finestra(da_istante: datetime | None) -> Any:
    """Entries to process: new or changed after the instant, or with newer rows."""
    if da_istante is None:
        return None
    return or_(
        Memory.created_at > da_istante,
        Memory.updated_at > da_istante,
        exists().where(
            MemoryFragment.memory_id == Memory.id, MemoryFragment.created_at > da_istante
        ),
        exists().where(MemoryHistory.memory_id == Memory.id, MemoryHistory.created_at > da_istante),
    )


async def _memorie_sorgente(
    sessione: AsyncSession, project_id: int, da_istante: datetime | None
) -> list[Memory]:
    stmt = select(Memory).where(Memory.project_id == project_id).order_by(Memory.id)
    filtro = _filtro_finestra(da_istante)
    if filtro is not None:
        stmt = stmt.where(filtro)
    return list((await sessione.execute(stmt)).scalars().all())


async def _copia_progetto(
    sorg: AsyncSession,
    dest: AsyncSession,
    team_dest: int,
    slug: str,
    project_sorg: int,
    da_istante: datetime | None,
    scrivi: bool,
) -> tuple[Conteggi, Conteggi]:
    """Copy one mapped project. Returns (in source, to write)."""
    in_sorgente = Conteggi()
    da_scrivere = Conteggi()
    memorie = await _memorie_sorgente(sorg, project_sorg, da_istante)
    if not memorie:
        return in_sorgente, da_scrivere

    project_dest = (
        await dest.execute(
            select(Project.id).where(Project.team_id == team_dest, Project.slug == slug)
        )
    ).scalar_one_or_none()
    if project_dest is None and scrivi:
        progetto = Project(team_id=team_dest, slug=slug)
        dest.add(progetto)
        await dest.flush()
        project_dest = progetto.id

    esistenti: dict[str, Memory] = {}
    if project_dest is not None:
        righe = await dest.execute(select(Memory).where(Memory.project_id == project_dest))
        esistenti = {m.keyword: m for m in righe.scalars().all()}

    for m in memorie:
        frammenti = list(
            (
                await sorg.execute(
                    select(MemoryFragment)
                    .where(MemoryFragment.memory_id == m.id)
                    .order_by(MemoryFragment.id)
                )
            )
            .scalars()
            .all()
        )
        storico = list(
            (
                await sorg.execute(
                    select(MemoryHistory)
                    .where(MemoryHistory.memory_id == m.id)
                    .order_by(MemoryHistory.version)
                )
            )
            .scalars()
            .all()
        )
        if m.deleted_at is None:
            in_sorgente.attive += 1
        else:
            in_sorgente.cancellate += 1
        in_sorgente.frammenti += len(frammenti)
        in_sorgente.storico += len(storico)

        gia = esistenti.get(m.keyword)
        azione = decidi_memoria(
            StatoMemoria(m.version, m.deleted_at is not None),
            None if gia is None else StatoMemoria(gia.version, gia.deleted_at is not None),
        )
        if azione is Azione.AGGIORNA and scrivi:
            assert gia is not None
            for nome in _COLONNE_AGGIORNAMENTO:
                setattr(gia, nome, getattr(m, nome))
        elif azione is Azione.INSERISCI and scrivi:
            gia = Memory(
                project_id=project_dest,
                keyword=m.keyword,
                **{nome: getattr(m, nome) for nome in _COLONNE_MEMORIA},
            )
            dest.add(gia)
            await dest.flush()
        if azione is not Azione.NULLA:
            if m.deleted_at is None:
                da_scrivere.attive += 1
            else:
                da_scrivere.cancellate += 1

        # Fragments and history are matched by their own natural keys, even
        # when the entry itself is left alone (a fragment can arrive alone).
        frammenti_dest: list[MemoryFragment] = []
        versioni_dest: set[int] = set()
        if gia is not None and gia.id is not None:
            frammenti_dest = list(
                (
                    await dest.execute(
                        select(MemoryFragment).where(MemoryFragment.memory_id == gia.id)
                    )
                )
                .scalars()
                .all()
            )
            versioni_dest = set(
                (
                    await dest.execute(
                        select(MemoryHistory.version).where(MemoryHistory.memory_id == gia.id)
                    )
                )
                .scalars()
                .all()
            )
        chiavi_dest = {chiave_frammento(f.created_at, f.content) for f in frammenti_dest}
        per_chiave = {chiave_frammento(f.created_at, f.content): f for f in frammenti}
        nuovi_frammenti = mancanti(
            (chiave_frammento(f.created_at, f.content) for f in frammenti), chiavi_dest
        )
        nuove_versioni = mancanti((h.version for h in storico), versioni_dest)
        da_scrivere.frammenti += len(nuovi_frammenti)
        da_scrivere.storico += len(nuove_versioni)

        if not scrivi:
            continue
        assert gia is not None
        if azione is Azione.AGGIORNA:
            # The entry was compacted on the source: fragments it consumed are
            # gone there, so they must not linger on the destination.
            chiavi_sorgente = set(per_chiave)
            da_togliere = [
                f.id
                for f in frammenti_dest
                if chiave_frammento(f.created_at, f.content) not in chiavi_sorgente
            ]
            if da_togliere:
                await dest.execute(delete(MemoryFragment).where(MemoryFragment.id.in_(da_togliere)))
        for chiave in nuovi_frammenti:
            f = per_chiave[chiave]
            dest.add(
                MemoryFragment(
                    memory_id=gia.id,
                    content=f.content,
                    token_count=f.token_count,
                    created_by=f.created_by,
                    created_at=f.created_at,
                )
            )
        daversione = {h.version: h for h in storico}
        for versione in nuove_versioni:
            h = daversione[versione]
            dest.add(
                MemoryHistory(
                    memory_id=gia.id,
                    version=h.version,
                    content=h.content,
                    updated_by=h.updated_by,
                    created_at=h.created_at,
                )
            )
        await dest.flush()
    return in_sorgente, da_scrivere


async def _copia_con_motori(
    motore_sorg: AsyncEngine,
    motore_dest: AsyncEngine,
    mappa: Mappa,
    da_istante: datetime | None,
    solo_conteggi: bool,
) -> Rapporto:
    rapporto = Rapporto(solo_conteggi=solo_conteggi)
    scrivi = not solo_conteggi
    sessione_sorg = async_sessionmaker(motore_sorg, expire_on_commit=False)
    sessione_dest = async_sessionmaker(motore_dest, expire_on_commit=False)
    async with sessione_sorg() as sorg, sessione_dest() as dest:
        team_sorg = await _team_id(sorg, mappa.team, "sorgente")
        team_dest = await _team_id(dest, mappa.team, "destinazione")
        progetti = (
            await sorg.execute(select(Project.id, Project.slug).where(Project.team_id == team_sorg))
        ).all()
        # The refusal comes BEFORE any write: nothing has touched the destination yet.
        verifica_decisioni([p.slug for p in progetti], mappa)
        try:
            for progetto in sorted(progetti, key=lambda p: p.slug):
                if progetto.slug not in mappa.mappati:
                    continue
                in_sorgente, da_scrivere = await _copia_progetto(
                    sorg, dest, team_dest, progetto.slug, progetto.id, da_istante, scrivi
                )
                rapporto.per_slug[progetto.slug] = in_sorgente
                rapporto.da_scrivere[progetto.slug] = da_scrivere
            if scrivi:
                await dest.commit()
            else:
                await dest.rollback()
        except BaseException:
            await dest.rollback()
            raise
    return rapporto


async def copia(
    sorgente: str | AsyncEngine,
    destinazione: str | AsyncEngine,
    mappa: Mappa,
    da_istante: datetime | None = None,
    *,
    solo_conteggi: bool = False,
) -> Rapporto:
    """Copy the mapped slugs of ``mappa.team`` from ``sorgente`` to ``destinazione``.

    ``sorgente``/``destinazione`` are database URLs or ready async engines
    (engines passed in are not disposed). One transaction on the destination.
    """
    propri: list[AsyncEngine] = []

    def _motore(valore: str | AsyncEngine) -> AsyncEngine:
        if isinstance(valore, str):
            motore = create_async_engine(url_async(valore))
            propri.append(motore)
            return motore
        return valore

    try:
        return await _copia_con_motori(
            _motore(sorgente), _motore(destinazione), mappa, da_istante, solo_conteggi
        )
    finally:
        for motore in propri:
            await motore.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        mappa = carica_mappa(args.mappa)
        rapporto = asyncio.run(
            copia(
                args.sorgente,
                args.destinazione,
                mappa,
                args.da_istante,
                solo_conteggi=args.solo_conteggi,
            )
        )
    except ErroreCopia as exc:
        print(f"errore: {exc}", file=sys.stderr)
        return 2
    print(formatta_rapporto(rapporto))
    return 0


if __name__ == "__main__":
    sys.exit(main())
