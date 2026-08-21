"""Dukascopy-Decoder.

Der wichtigste Test ist die FELDREIHENFOLGE. Dukascopy liefert
(open, close, low, high) - nicht OHLC. Liest man es als OHLC, entstehen
lautlos unsinnige Kerzen: bei einer echten Stichprobe verletzten 1281 von
1329 Kerzen die Bedingung low <= open, close <= high. Nichts wuerde
abstuerzen, aber jede darauf gebaute Analyse waere wertlos.

Alle Tests laufen offline gegen selbst erzeugte Nutzdaten - kein Netzzugriff.
"""

from __future__ import annotations

import lzma
import struct
from datetime import UTC, datetime

import pytest

from tradex.data.dukascopy_provider import (
    _MAX_ATTEMPTS,
    PRICE_DIVIDER,
    day_url,
    decode_day,
    fetch_day,
    has_data,
)
from tradex.data.provider import ProviderError
from tradex.domain.bars import NS_PER_SECOND

DAY = datetime(2025, 3, 19, tzinfo=UTC)


def _record(offset: int, open_: float, close: float, low: float, high: float, volume: float) -> bytes:
    """Einen Satz im Dukascopy-Format bauen: offset, open, close, low, high, volume."""
    return struct.pack(
        ">Iiiiif",
        offset,
        round(open_ * PRICE_DIVIDER),
        round(close * PRICE_DIVIDER),
        round(low * PRICE_DIVIDER),
        round(high * PRICE_DIVIDER),
        volume,
    )


def _payload(records: list[bytes]) -> bytes:
    return lzma.compress(b"".join(records))


# ------------------------------------------------------------------------ URL
def test_monat_ist_nullbasiert():
    """Dukascopy zaehlt Monate ab 0 - Maerz ist 02, nicht 03.

    Ein Off-by-one holt hier klaglos die Daten des Vormonats.
    """
    url = day_url("USATECHIDXUSD", DAY)
    assert url.endswith("/2025/02/19/BID_candles_min_1.bi5")
    assert "/03/" not in url


def test_januar_wird_zu_null():
    url = day_url("USATECHIDXUSD", datetime(2024, 1, 5, tzinfo=UTC))
    assert url.endswith("/2024/00/05/BID_candles_min_1.bi5")


# -------------------------------------------------------------- Feldreihenfolge
def test_feldreihenfolge_ist_open_close_low_high():
    """Die entscheidende Zusicherung dieses Moduls."""
    rows = decode_day(
        _payload([_record(0, open_=100.0, close=103.0, low=99.0, high=105.0, volume=1.5)]),
        DAY,
    )

    assert rows.size == 1
    assert rows[0]["open"] == pytest.approx(100.0)
    assert rows[0]["close"] == pytest.approx(103.0)
    assert rows[0]["low"] == pytest.approx(99.0)
    assert rows[0]["high"] == pytest.approx(105.0)


def test_dekodierte_kerzen_sind_physikalisch_moeglich():
    """Bei falscher Reihenfolge waere genau das verletzt."""
    records = [
        _record(0, 100.0, 103.0, 99.0, 105.0, 1.0),
        _record(60, 103.0, 101.5, 100.5, 104.0, 2.0),
        _record(120, 101.5, 101.5, 101.5, 101.5, 0.5),
    ]
    rows = decode_day(_payload(records), DAY)

    assert rows.size == 3
    for row in rows:
        assert row["low"] <= row["open"] <= row["high"]
        assert row["low"] <= row["close"] <= row["high"]
        assert row["high"] >= row["low"]


# ------------------------------------------------------------------ Zeitstempel
def test_zeitstempel_sind_tagesbeginn_plus_versatz():
    rows = decode_day(
        _payload(
            [
                _record(0, 100.0, 100.0, 100.0, 100.0, 1.0),
                _record(60, 100.0, 100.0, 100.0, 100.0, 1.0),
                _record(3600, 100.0, 100.0, 100.0, 100.0, 1.0),
            ]
        ),
        DAY,
    )
    day_start = int(DAY.timestamp()) * NS_PER_SECOND

    assert int(rows[0]["ts"]) == day_start
    assert int(rows[1]["ts"]) == day_start + 60 * NS_PER_SECOND
    assert int(rows[2]["ts"]) == day_start + 3600 * NS_PER_SECOND


def test_preisteiler():
    """Rohwert 19495332 entspricht 19495.332 - passend zum Nasdaq-100."""
    raw = struct.pack(">Iiiiif", 0, 19495332, 19498287, 19493698, 19504065, 0.02)
    rows = decode_day(lzma.compress(raw), DAY)
    assert rows[0]["open"] == pytest.approx(19495.332)
    assert rows[0]["high"] == pytest.approx(19504.065)


# ----------------------------------------------------------------- Fuellminuten
def test_fuellminuten_werden_verworfen():
    """Dukascopy liefert 1440 Saetze je Tag, auch fuer handelsfreie Zeiten.

    Wuerden sie uebernommen, stuenden Scheinkerzen in der Historie und
    verfaelschten Swings, Sessions und Liquiditaetslevel.
    """
    records = [
        _record(0, 100.0, 100.0, 100.0, 100.0, 0.0),  # Fuellminute
        _record(60, 100.0, 103.0, 99.0, 105.0, 1.5),  # echt
        _record(120, 103.0, 103.0, 103.0, 103.0, 0.0),  # Fuellminute
    ]
    rows = decode_day(_payload(records), DAY)

    assert rows.size == 1
    assert rows[0]["volume"] == pytest.approx(1.5)


def test_tag_ganz_ohne_handel():
    records = [_record(i * 60, 100.0, 100.0, 100.0, 100.0, 0.0) for i in range(10)]
    assert decode_day(_payload(records), DAY).size == 0


def test_leere_datei_ist_kein_fehler():
    """Wochenenden und Feiertage liefern eine leere Antwort."""
    assert decode_day(b"", DAY).size == 0


# ---------------------------------------------------------------- Fehlerfaelle
def test_beschaedigte_datei_wird_gemeldet():
    with pytest.raises(ProviderError, match="beschaedigt"):
        decode_day(b"kein gueltiges lzma", DAY)


def test_unerwartete_dateigroesse_wird_gemeldet():
    """Ein halber Satz deutet auf ein geaendertes Format hin - das darf nicht
    stillschweigend als Daten durchgehen."""
    with pytest.raises(ProviderError, match="Vielfaches"):
        decode_day(lzma.compress(b"\x00" * 30), DAY)


def test_404_liefert_leeren_inhalt(monkeypatch: pytest.MonkeyPatch):
    """Fuer Tage ohne Daten antwortet Dukascopy mit 404 - kein Fehlerfall."""
    import urllib.error

    def raise_404(*_args, **_kwargs):
        raise urllib.error.HTTPError("url", 404, "Not Found", None, None)

    monkeypatch.setattr("urllib.request.urlopen", raise_404)
    assert fetch_day("USATECHIDXUSD", DAY) == b""


def test_netzfehler_wird_nach_wiederholungen_gemeldet(monkeypatch: pytest.MonkeyPatch):
    attempts = {"n": 0}

    def raise_timeout(*_args, **_kwargs):
        attempts["n"] += 1
        raise TimeoutError("read timed out")

    monkeypatch.setattr("urllib.request.urlopen", raise_timeout)
    monkeypatch.setattr("time.sleep", lambda _s: None)

    with pytest.raises(ProviderError, match="nicht erreichbar"):
        fetch_day("USATECHIDXUSD", DAY)
    # Gegen die Konstante pruefen, nicht gegen eine Zahl: sonst bricht der Test,
    # sobald die Wiederholungsstrategie sinnvoll angepasst wird.
    assert attempts["n"] == _MAX_ATTEMPTS, "es muss mehrfach versucht werden"
    assert _MAX_ATTEMPTS > 1


# ------------------------------------------------------------- Handelstage
def test_samstage_werden_nicht_angefragt():
    """Samstags ruht der Handel - jede Anfrage waere vergeudet.

    Sonntags oeffnet Globex dagegen um 17:00 Boersenzeit, dort gibt es Daten.
    """
    saturday = datetime(2025, 3, 22, tzinfo=UTC)
    sunday = datetime(2025, 3, 23, tzinfo=UTC)
    monday = datetime(2025, 3, 24, tzinfo=UTC)

    assert saturday.weekday() == 5
    assert not has_data(saturday)
    assert has_data(sunday)
    assert has_data(monday)


# ------------------------------------------------------------------ Integration
def test_kerzen_lassen_sich_zu_einer_barserie_fuegen():
    """Die dekodierten Werte muessen die Pruefungen der BarSeries bestehen."""
    from tradex.domain.bars import Bar, BarSeries

    records = [
        _record(0, 21000.0, 21003.0, 20999.0, 21005.0, 1.0),
        _record(60, 21003.0, 21001.5, 21000.5, 21004.0, 2.0),
    ]
    rows = decode_day(_payload(records), DAY)

    series = BarSeries()
    for row in rows:
        series.append(
            ts=int(row["ts"]),
            open_=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
        )

    assert len(series) == 2
    for i in range(len(series)):
        bar: Bar = series[i]
        bar.validate()  # wirft bei unmoeglichen Kerzen
