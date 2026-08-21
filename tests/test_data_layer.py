"""Datenschicht: Kalender, Integritaet, Bar-Store, Rolls."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from tests.conftest import make_series
from tradex.data.integrity import check, find_gaps, find_invalid_bars
from tradex.data.rolls import mark_rolls_from_symbols, quarterly_expiries, third_friday
from tradex.data.sessions import SessionCalendar, SessionResolver
from tradex.data.store import BarStore
from tradex.domain.bars import BarSeries, from_ns, to_ns
from tradex.domain.enums import SessionName, Timeframe
from tradex.domain.instruments import Instrument


# ------------------------------------------------------------------ Kalender
@pytest.mark.parametrize(
    ("utc_time", "expected"),
    [
        ("2025-03-04 01:00", SessionName.ASIA),  # 19:00 CT
        ("2025-03-04 09:00", SessionName.LONDON),  # 03:00 CT
        ("2025-03-04 15:00", SessionName.NY_AM),  # 09:00 CT
        ("2025-03-04 19:00", SessionName.NY_PM),  # 13:00 CT
        ("2025-03-04 22:30", SessionName.CLOSED),  # 16:30 CT - Wartungspause
    ],
)
def test_session_zuordnung(mnq: Instrument, utc_time: str, expected: SessionName):
    ts = to_ns(datetime.strptime(utc_time, "%Y-%m-%d %H:%M").replace(tzinfo=UTC))
    calendar = SessionCalendar(mnq)
    assert calendar.sessions(np.array([ts]))[0] == expected.value


def test_sessions_decken_den_handelstag_lueckenlos_ab(mnq: Instrument):
    """session == CLOSED muss genau dann gelten, wenn der Markt geschlossen ist.

    Bleibt eine Zeitspanne von keinem Session-Fenster abgedeckt, meldet der
    Kalender dort "geschlossen", waehrend die Handelszeiten "offen" sagen. Die
    Folge waeren Phantom-Datenluecken und falsche Session-High/Low-Level.
    Genau dieser Fall (17:00-18:00 CT gehoerte zu keiner Session) ist beim
    ersten Analyselauf aufgefallen.
    """
    calendar = SessionCalendar(mnq)
    start = datetime(2025, 3, 3, 0, 0, tzinfo=UTC)
    stamps = np.array(
        [to_ns(start + timedelta(minutes=i)) for i in range(60 * 24 * 7)], dtype=np.int64
    )

    is_open = calendar.is_open(stamps)
    is_closed_session = calendar.sessions(stamps) == SessionName.CLOSED.value

    mismatches = np.flatnonzero(is_open & is_closed_session)
    assert mismatches.size == 0, (
        f"{mismatches.size} Minuten ohne Session trotz offenem Markt, "
        f"erste bei {from_ns(int(stamps[mismatches[0]]))} UTC"
        if mismatches.size
        else ""
    )


def test_handelstag_beginnt_um_17_uhr_ct(mnq: Instrument):
    """Eine Bar von Montag 18:00 CT gehoert bereits zum Handelstag Dienstag."""
    calendar = SessionCalendar(mnq)
    monday_evening = to_ns(datetime(2025, 3, 4, 0, 0, tzinfo=UTC))  # Mo 18:00 CT
    monday_morning = to_ns(datetime(2025, 3, 3, 16, 0, tzinfo=UTC))  # Mo 10:00 CT

    assert str(calendar.trading_day(np.array([monday_evening]))[0]) == "2025-03-04"
    assert str(calendar.trading_day(np.array([monday_morning]))[0]) == "2025-03-03"


def test_markt_ist_am_wochenende_geschlossen(mnq: Instrument):
    calendar = SessionCalendar(mnq)
    saturday = to_ns(datetime(2025, 3, 8, 12, 0, tzinfo=UTC))
    friday_evening = to_ns(datetime(2025, 3, 7, 23, 0, tzinfo=UTC))  # Fr 17:00 CT
    sunday_evening = to_ns(datetime(2025, 3, 9, 23, 0, tzinfo=UTC))  # So 18:00 CDT

    assert not calendar.is_open(np.array([saturday]))[0]
    assert not calendar.is_open(np.array([friday_evening]))[0]
    assert calendar.is_open(np.array([sunday_evening]))[0]


def test_resolver_entspricht_kalender(mnq: Instrument):
    """Der schnelle skalare Pfad darf nie vom vektorisierten abweichen.

    Geprueft ueber eine Woche inklusive Sommerzeitwechsel am 2025-03-09.
    """
    start = datetime(2025, 3, 6, 0, 0, tzinfo=UTC)
    stamps = np.array(
        [to_ns(start + timedelta(minutes=7 * i)) for i in range(2000)], dtype=np.int64
    )
    calendar = SessionCalendar(mnq)
    resolver = SessionResolver(mnq)

    expected_sessions = calendar.sessions(stamps)
    expected_days = calendar.trading_day(stamps)

    for i, ts in enumerate(stamps):
        session, day_ord, week_ord = resolver.resolve(int(ts))
        assert session == expected_sessions[i], f"Session bei {ts}"
        assert datetime.fromordinal(day_ord).date().isoformat() == str(expected_days[i])
        assert week_ord <= day_ord


# ---------------------------------------------------------------- Integritaet
def test_findet_luecke_in_offener_marktzeit(mnq: Instrument):
    series = BarSeries()
    start = datetime(2025, 3, 4, 15, 0, tzinfo=UTC)  # Di 09:00 CT, Markt offen
    for i in [0, 1, 2, 8, 9]:  # 5 fehlende Minuten
        series.append(to_ns(start + timedelta(minutes=i)), 100, 101, 99, 100, 10)

    gaps = find_gaps(series, Timeframe.M1, SessionCalendar(mnq))
    assert len(gaps) == 1
    assert gaps[0].missing_bars == 5


def test_wochenende_ist_keine_luecke(mnq: Instrument):
    """Sonst wuerde jede Handelswoche als Datenfehler gemeldet."""
    series = BarSeries()
    friday_close = datetime(2025, 3, 7, 21, 59, tzinfo=UTC)  # Fr 15:59 CT
    sunday_open = datetime(2025, 3, 9, 22, 0, tzinfo=UTC)  # So 17:00 CDT
    series.append(to_ns(friday_close), 100, 101, 99, 100, 10)
    series.append(to_ns(sunday_open), 100, 101, 99, 100, 10)

    assert find_gaps(series, Timeframe.M1, SessionCalendar(mnq)) == ()


def test_findet_unmoegliche_bars():
    series = make_series(
        [
            (100, 101, 99, 100),
            (100, 99, 101, 100),  # High < Low
            (100, 101, 99, 105),  # Close ausserhalb der Range
            (100, 101, 99, 100),
        ]
    )
    assert find_invalid_bars(series) == (1, 2)


def test_integritaetsbericht(mnq: Instrument):
    series = make_series([(100, 101, 99, 100)] * 10)
    report = check(series, "MNQ", Timeframe.M1, SessionCalendar(mnq))
    assert report.is_clean
    assert report.bar_count == 10
    assert "keine Auffaelligkeiten" in report.summary()


# --------------------------------------------------------------------- Rolls
def test_rollgrenzen_aus_kontraktwechsel():
    symbols = np.array(["MNQH5", "MNQH5", "MNQM5", "MNQM5", "MNQU5"])
    result = mark_rolls_from_symbols(symbols)
    assert list(result) == [False, False, True, False, True]
    assert not result[0], "Die erste Bar hat keinen Vorgaenger"


def test_dritter_freitag():
    assert third_friday(2025, 3) == datetime(2025, 3, 21).date()
    assert third_friday(2025, 6) == datetime(2025, 6, 20).date()
    assert third_friday(2025, 12) == datetime(2025, 12, 19).date()


def test_quartalsverfall():
    expiries = quarterly_expiries(2025, 2025)
    assert len(expiries) == 4
    assert [d.month for d in expiries] == [3, 6, 9, 12]


# ----------------------------------------------------------------- Bar-Store
def test_store_schreibt_und_liest(tmp_path: Path):
    store = BarStore(tmp_path)
    series = make_series([(100 + i, 101 + i, 99 + i, 100 + i, 10 * i) for i in range(100)])

    assert store.write("MNQ", Timeframe.M1, series) == 100
    loaded = store.read("MNQ", Timeframe.M1)

    assert len(loaded) == 100
    assert np.array_equal(loaded.ts, series.ts)
    assert np.allclose(loaded.close, series.close)


def test_store_ist_idempotent(tmp_path: Path):
    """Downloads brechen ab und werden wiederholt - doppeltes Schreiben darf nichts aendern."""
    store = BarStore(tmp_path)
    series = make_series([(100, 101, 99, 100)] * 50)

    store.write("MNQ", Timeframe.M1, series)
    store.write("MNQ", Timeframe.M1, series)

    assert len(store.read("MNQ", Timeframe.M1)) == 50


def test_store_ueberschreibt_bei_gleichem_timestamp(tmp_path: Path):
    store = BarStore(tmp_path)
    store.write("MNQ", Timeframe.M1, make_series([(100, 101, 99, 100)] * 10))
    store.write("MNQ", Timeframe.M1, make_series([(200, 201, 199, 200)] * 10))

    loaded = store.read("MNQ", Timeframe.M1)
    assert len(loaded) == 10
    assert loaded[0].close == 200.0, "Der neuere Wert gewinnt"


def test_store_zeitfilter_und_limit(tmp_path: Path):
    store = BarStore(tmp_path)
    series = make_series([(100, 101, 99, 100)] * 100)
    store.write("MNQ", Timeframe.M1, series)

    window = store.read("MNQ", Timeframe.M1, int(series.ts[10]), int(series.ts[20]))
    assert len(window) == 10, "end_ts ist exklusiv"

    latest = store.read("MNQ", Timeframe.M1, limit=5)
    assert len(latest) == 5
    assert int(latest.ts[-1]) == int(series.ts[-1]), "limit liefert die juengsten Bars"


def test_store_coverage_und_leerzustand(tmp_path: Path):
    store = BarStore(tmp_path)
    assert store.coverage("MNQ", Timeframe.M1) is None
    assert len(store.read("MNQ", Timeframe.M1)) == 0
    assert store.symbols() == []

    series = make_series([(100, 101, 99, 100)] * 42)
    store.write("MNQ", Timeframe.M1, series)

    coverage = store.coverage("MNQ", Timeframe.M1)
    assert coverage is not None
    assert coverage.bar_count == 42
    assert coverage.first_ts == int(series.ts[0])
    assert store.symbols() == ["MNQ"]
    assert store.timeframes("MNQ") == [Timeframe.M1]


def test_store_monatsdateien(tmp_path: Path):
    """Ablage in Monatsdateien haelt Upserts klein und die Dateizahl beherrschbar."""
    store = BarStore(tmp_path)
    start = datetime(2025, 1, 30, 12, 0, tzinfo=UTC)
    series = BarSeries()
    for i in range(60 * 24 * 4):  # ueber den Monatswechsel hinweg
        series.append(to_ns(start + timedelta(minutes=i)), 100, 101, 99, 100, 1)
    store.write("MNQ", Timeframe.M1, series)

    files = sorted(p.name for p in store.dir_for("MNQ", Timeframe.M1).glob("*.parquet"))
    assert files == ["part-2025-01.parquet", "part-2025-02.parquet"]
