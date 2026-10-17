"""scripts.copy_memories — the parts that need no database.

Mapping loader, the "slug without a decision" refusal, natural-key diff
planning, report and argument parsing. The end-to-end copy against two real
Postgres databases is in ``test_copy_memories.py`` [DOCKER].
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import copy_memories as cm
from scripts.copy_memories import (
    Azione,
    Conteggi,
    ErroreCopia,
    Mappa,
    PianoMemoria,
    Rapporto,
    RigaMemoria,
    SlugSenzaDecisione,
    StatoMemoria,
)

MAPPA_REALE = (
    Path(__file__).resolve().parents[3]
    / "codepress-workspace"
    / "deploy"
    / "memoria"
    / "mappatura.json"
)


def _dati() -> dict:
    return {
        "team": "codepress",
        "progetti": [
            {"chiave": "WSP", "slug": [{"slug": "codepress-workspace", "voci": 66}]},
            {
                "chiave": "COD",
                "slug": [{"slug": "soluzione-business", "voci": 254}, {"slug": "srl", "voci": 3}],
            },
        ],
        "scartati": [{"slug": "tmp", "voci": 3}, {"slug": "memory", "voci": 6}],
    }


# --- mapping loader ---------------------------------------------------------


def test_mappa_da_dati_reads_team_mapped_and_discarded() -> None:
    mappa = cm.mappa_da_dati(_dati())
    assert mappa.team == "codepress"
    assert mappa.mappati == {"codepress-workspace", "soluzione-business", "srl"}
    assert mappa.scartati == {"tmp", "memory"}


def test_mappa_without_scartati_is_valid() -> None:
    dati = _dati()
    del dati["scartati"]
    assert cm.mappa_da_dati(dati).scartati == frozenset()


@pytest.mark.parametrize(
    "rompi",
    [
        lambda d: d.pop("team"),
        lambda d: d.update(team=""),
        lambda d: d.update(team=3),
        lambda d: d.pop("progetti"),
        lambda d: d.update(progetti={}),
        lambda d: d["progetti"].append("x"),
        lambda d: d["progetti"].append({"chiave": "X"}),
        lambda d: d["progetti"][0].update(slug=[{"voci": 1}]),
        lambda d: d["scartati"].append({"slug": ""}),
        lambda d: d.update(scartati="tmp"),
    ],
)
def test_mappa_malformed_is_refused(rompi) -> None:
    dati = _dati()
    rompi(dati)
    with pytest.raises(ErroreCopia):
        cm.mappa_da_dati(dati)


def test_mappa_root_must_be_object() -> None:
    with pytest.raises(ErroreCopia):
        cm.mappa_da_dati([])


def test_mappa_slug_both_mapped_and_discarded_is_refused() -> None:
    dati = _dati()
    dati["scartati"].append({"slug": "srl"})
    with pytest.raises(ErroreCopia, match="srl"):
        cm.mappa_da_dati(dati)


def test_carica_mappa_reads_file_and_refuses_bad_ones(tmp_path: Path) -> None:
    ok = tmp_path / "m.json"
    ok.write_text(json.dumps(_dati()), encoding="utf-8")
    assert cm.carica_mappa(ok).team == "codepress"
    with pytest.raises(ErroreCopia):
        cm.carica_mappa(tmp_path / "manca.json")
    rotto = tmp_path / "rotto.json"
    rotto.write_text("{", encoding="utf-8")
    with pytest.raises(ErroreCopia):
        cm.carica_mappa(rotto)


@pytest.mark.skipif(not MAPPA_REALE.exists(), reason="Workspace repo not next to this one")
def test_loader_reads_the_real_mapping_file() -> None:
    mappa = cm.carica_mappa(MAPPA_REALE)
    assert mappa.team == "codepress"
    assert {"codepress-workspace", "soluzione-business", "srl"} <= mappa.mappati
    assert "tmp" in mappa.scartati
    assert not mappa.mappati & mappa.scartati


# --- "slug without a decision" ---------------------------------------------------


def test_slug_senza_decisione_lists_only_unresolved_sorted() -> None:
    mappa = cm.mappa_da_dati(_dati())
    sorgente = ["srl", "tmp", "zeta", "alfa", "codepress-workspace"]
    assert cm.slug_senza_decisione(sorgente, mappa) == ["alfa", "zeta"]


def test_verifica_decisioni_passes_when_every_slug_has_a_decision() -> None:
    mappa = cm.mappa_da_dati(_dati())
    cm.verifica_decisioni(["srl", "tmp", "memory"], mappa)  # no raise
    cm.verifica_decisioni([], mappa)


def test_verifica_decisioni_refuses_and_names_the_slugs() -> None:
    mappa = cm.mappa_da_dati(_dati())
    with pytest.raises(SlugSenzaDecisione) as info:
        cm.verifica_decisioni(["srl", "ignoto-1", "ignoto-2"], mappa)
    assert info.value.slugs == ["ignoto-1", "ignoto-2"]
    messaggio = str(info.value)
    assert "ignoto-1" in messaggio and "ignoto-2" in messaggio
    assert "srl" not in messaggio
    assert isinstance(info.value, ErroreCopia)


def test_copy_checks_decisions_before_the_first_write() -> None:
    """A guard that READS the code: the refusal precedes any per-project copy."""
    sorgente = inspect.getsource(cm._copia_con_motori)
    assert "verifica_decisioni(" in sorgente
    assert sorgente.index("verifica_decisioni(") < sorgente.index("_copia_progetto(")
    assert sorgente.index("verifica_decisioni(") < sorgente.index(".commit()")


def test_copy_never_creates_a_team() -> None:
    sorgente = inspect.getsource(cm)
    assert "Team(" not in sorgente
    assert "create_team" not in sorgente


# --- natural-key diff ---------------------------------------------------------------


def test_decidi_memoria_absent_on_destination_inserts() -> None:
    assert cm.decidi_memoria(StatoMemoria(1, False), None) is Azione.INSERISCI
    assert cm.decidi_memoria(StatoMemoria(4, True), None) is Azione.INSERISCI


def test_decidi_memoria_higher_source_version_updates() -> None:
    assert cm.decidi_memoria(StatoMemoria(3, False), StatoMemoria(2, False)) is Azione.AGGIORNA


def test_decidi_memoria_same_or_lower_version_is_left_alone() -> None:
    assert cm.decidi_memoria(StatoMemoria(2, False), StatoMemoria(2, False)) is Azione.NULLA
    assert cm.decidi_memoria(StatoMemoria(1, False), StatoMemoria(2, False)) is Azione.NULLA


def test_decidi_memoria_delete_on_source_at_same_version_updates_once() -> None:
    assert cm.decidi_memoria(StatoMemoria(2, True), StatoMemoria(2, False)) is Azione.AGGIORNA
    # idempotent: once deleted on both sides there is nothing left to do
    assert cm.decidi_memoria(StatoMemoria(2, True), StatoMemoria(2, True)) is Azione.NULLA
    # a delete on the destination is never undone by an older live source
    assert cm.decidi_memoria(StatoMemoria(2, False), StatoMemoria(2, True)) is Azione.NULLA


def test_mancanti_keeps_source_order_and_dedups() -> None:
    assert cm.mancanti([3, 1, 3, 2, 1], {2}) == [3, 1]
    assert cm.mancanti([], {1}) == []


def test_chiave_frammento_is_time_and_content() -> None:
    t = datetime(2026, 1, 1, tzinfo=UTC)
    assert cm.chiave_frammento(t, "x") == cm.chiave_frammento(t, "x")
    assert cm.chiave_frammento(t, "x") != cm.chiave_frammento(t + timedelta(microseconds=1), "x")
    assert cm.chiave_frammento(t, "x") != cm.chiave_frammento(t, "y")


# --- report -------------------------------------------------------------------------------


def test_rapporto_totale_and_format() -> None:
    r = Rapporto(
        per_slug={"a": Conteggi(2, 1, 3, 5), "b": Conteggi(1, 0, 0, 1)},
        da_scrivere={"a": Conteggi(2, 1, 3, 5)},
        solo_conteggi=True,
    )
    assert r.totale() == Conteggi(3, 1, 3, 6)
    testo = cm.formatta_rapporto(r)
    assert "a: 2 1 3 5 | 2 1 3 5" in testo
    assert "b: 1 0 0 1 | 0 0 0 0" in testo
    assert "totale: 3 1 3 6" in testo
    assert "nessuna scrittura" in testo


def test_rapporto_empty() -> None:
    assert Rapporto().totale() == Conteggi()
    assert "nessuna scrittura" not in cm.formatta_rapporto(Rapporto())


# --- arguments -----------------------------------------------------------------------------


def test_parse_args_full() -> None:
    args = cm.parse_args(
        [
            "--from",
            "postgresql://a/b",
            "--to",
            "postgresql+asyncpg://c/d",
            "--mappa",
            "m.json",
            "--da-istante",
            "2026-10-17T10:00:00+00:00",
            "--solo-conteggi",
        ]
    )
    assert args.sorgente == "postgresql://a/b"
    assert args.destinazione == "postgresql+asyncpg://c/d"
    assert args.mappa == "m.json"
    assert args.da_istante == datetime(2026, 10, 17, 10, tzinfo=UTC)
    assert args.solo_conteggi is True


def test_parse_args_defaults() -> None:
    args = cm.parse_args(["--from", "x", "--to", "y", "--mappa", "m"])
    assert args.da_istante is None
    assert args.solo_conteggi is False


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--from", "x", "--to", "y"],
        ["--from", "x", "--mappa", "m"],
        ["--to", "y", "--mappa", "m"],
        ["--from", "x", "--to", "y", "--mappa", "m", "--da-istante", "ieri"],
    ],
)
def test_parse_args_rejects_incomplete_or_bad(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as info:
        cm.parse_args(argv)
    assert info.value.code != 0


def test_parse_istante_naive_is_utc() -> None:
    assert cm.parse_istante("2026-10-17T10:00:00") == datetime(2026, 10, 17, 10, tzinfo=UTC)


def test_url_async_adds_the_asyncpg_driver_only_when_needed() -> None:
    assert cm.url_async("postgresql://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"
    assert cm.url_async("postgres://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"
    assert cm.url_async("postgresql+asyncpg://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"


def test_main_missing_mapping_file_exits_nonzero_without_touching_a_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cm.main(
        ["--from", "postgresql://x/x", "--to", "postgresql://y/y", "--mappa", str(tmp_path / "no")]
    )
    assert code != 0
    assert "mappatura illeggibile" in capsys.readouterr().err


def test_mappa_is_immutable() -> None:
    mappa = Mappa("t", frozenset({"a"}), frozenset())
    with pytest.raises(AttributeError):
        mappa.team = "x"  # type: ignore[misc]


# --- generations ----------------------------------------------------------------------------

T0 = datetime(2026, 10, 1, tzinfo=UTC)
T1 = T0 + timedelta(days=5)


def _r(keyword: str, creata: datetime, version: int = 1, cancellata: bool = False) -> RigaMemoria:
    return RigaMemoria(keyword, creata, version, cancellata)


def test_keyword_base_strips_only_the_rename_suffix() -> None:
    assert cm.keyword_base("redis#deleted#12") == "redis"
    assert cm.keyword_base("redis") == "redis"
    assert cm.keyword_base("a#deleted#1#deleted#2") == "a#deleted#1"
    assert cm.keyword_base("redis#deleted#") == "redis#deleted#"
    assert cm.keyword_base("redis#deleted#x") == "redis#deleted#x"
    assert cm.e_rinominata("redis#deleted#12") and not cm.e_rinominata("redis")


def test_keyword_da_parte_has_the_apps_form() -> None:
    assert cm.keyword_da_parte("k", 7) == "k#deleted#7"


def test_generation_identity_ignores_the_suffix_but_not_created_at() -> None:
    assert cm.identita_generazione("k#deleted#3", T0) == cm.identita_generazione("k", T0)
    assert cm.identita_generazione("k", T0) != cm.identita_generazione("k", T1)


def test_same_generation_follows_the_version_rules() -> None:
    piano = cm.pianifica_memoria(_r("k", T0, 3), _r("k", T0, 2), _r("k", T0, 2))
    assert piano == PianoMemoria(Azione.AGGIORNA)
    assert cm.pianifica_memoria(_r("k", T0, 2), _r("k", T0, 2), _r("k", T0, 2)).azione is (
        Azione.NULLA
    )


def test_same_key_different_created_at_is_a_new_generation_not_an_update() -> None:
    # The scenario of the review: deleted v3 on the destination, recreated v1 on the source.
    piano = cm.pianifica_memoria(_r("k", T1, 1), None, _r("k", T0, 3, cancellata=True))
    assert piano.azione is Azione.INSERISCI
    assert piano.metti_da_parte is True
    assert piano.cancella_occupante is False  # already deleted: keep its state
    # even with a higher source version there is no AGGIORNA across generations
    assert cm.pianifica_memoria(_r("k", T1, 9), None, _r("k", T0, 1)).azione is Azione.INSERISCI


def test_live_occupant_of_another_generation_is_deleted_when_set_aside() -> None:
    piano = cm.pianifica_memoria(_r("k", T1), None, _r("k", T0, 2, cancellata=False))
    assert piano == PianoMemoria(Azione.INSERISCI, metti_da_parte=True, cancella_occupante=True)


def test_free_keyword_is_a_plain_insert() -> None:
    assert cm.pianifica_memoria(_r("k", T1), None, None) == PianoMemoria(Azione.INSERISCI)


def test_renamed_source_row_matches_by_base_and_created_at() -> None:
    # Destination holds the same generation under its own suffix: nothing to do.
    sorgente = _r("k#deleted#5", T0, 3, cancellata=True)
    gia = _r("k#deleted#99", T0, 3, cancellata=True)
    assert cm.pianifica_memoria(sorgente, gia, None).azione is Azione.NULLA
    # ...or live on the destination at the same version: the delete is propagated.
    assert cm.pianifica_memoria(sorgente, _r("k", T0, 3), _r("k", T0, 3)).azione is (
        Azione.AGGIORNA
    )


def test_renamed_source_row_is_inserted_with_the_destination_suffix_never_its_own() -> None:
    piano = cm.pianifica_memoria(_r("k#deleted#5", T0, 3, cancellata=True), None, None)
    assert piano == PianoMemoria(Azione.INSERISCI, keyword_con_id_destinazione=True)
    assert piano.metti_da_parte is False  # the plain keyword is not in play


def test_second_run_plans_nothing() -> None:
    """After a run every source row has its generation on the destination."""
    sorgente = [
        _r("k#deleted#5", T0, 3, cancellata=True),
        _r("k", T1, 2),
        _r("altro", T0, 1),
    ]
    destinazione = [
        _r("k#deleted#77", T0, 3, cancellata=True),
        _r("k", T1, 2),
        _r("altro", T0, 1),
    ]
    per_gen = {cm.identita_generazione(d.keyword, d.created_at): d for d in destinazione}
    occupanti = {d.keyword: d for d in destinazione if not cm.e_rinominata(d.keyword)}
    for s in sorgente:
        piano = cm.pianifica_memoria(
            s,
            per_gen.get(cm.identita_generazione(s.keyword, s.created_at)),
            occupanti.get(s.keyword),
        )
        assert piano == PianoMemoria(Azione.NULLA), s


def test_generation_compare_is_what_separates_the_scenarios() -> None:
    """Guard that READS the code: the planner must compare created_at."""
    assert "created_at" in inspect.getsource(cm.identita_generazione)
    assert "identita_generazione(" in inspect.getsource(cm._copia_progetto)
    assert "pianifica_memoria(" in inspect.getsource(cm._copia_progetto)


# --- connection failures ------------------------------------------------------------------------


def test_main_connection_failure_is_one_short_line_without_url_or_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ok = tmp_path / "m.json"
    ok.write_text(json.dumps(_dati()), encoding="utf-8")

    def _rotto(*_a, **_k):
        raise OSError("password authentication failed for postgresql://u:segreto@h/db")

    monkeypatch.setattr(cm, "create_async_engine", _rotto)
    code = cm.main(
        ["--from", "postgresql://u:segreto@h/db", "--to", "postgresql://u:segreto@h/db"]
        + ["--mappa", str(ok)]
    )
    err = capsys.readouterr().err
    assert code != 0
    assert err.strip() == "errore: connessione fallita (sorgente)"
    assert "segreto" not in err and "Traceback" not in err


def test_main_unexpected_failure_never_leaks_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ok = tmp_path / "m.json"
    ok.write_text(json.dumps(_dati()), encoding="utf-8")

    async def _boom(*_a, **_k):
        raise RuntimeError("dettaglio con postgresql://u:segreto@h/db")

    monkeypatch.setattr(cm, "copia", _boom)
    code = cm.main(["--from", "x", "--to", "y", "--mappa", str(ok)])
    err = capsys.readouterr().err
    assert code != 0 and "segreto" not in err and "Traceback" not in err
    assert err.count(chr(10)) == 1


def test_source_session_is_read_only_repeatable_read() -> None:
    sorgente = inspect.getsource(cm._copia_con_motori)
    assert "REPEATABLE READ" in sorgente and "postgresql_readonly" in sorgente
