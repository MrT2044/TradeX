"""Nachrichtenfilter: sperrt er das Richtige - und merkt er, wenn er blind ist?

Kein Test hier geht ins Netz. Die Provider werden gegen festgehaltene Antworten
geprueft; was die Quelle heute liefert, ist keine Eigenschaft dieses Codes.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from tests.conftest import tradeable_config, trending_market
from tradex.analysis import reasons as R
from tradex.backtest.runner import Backtester
from tradex.config import Config, NewsConfig
from tradex.domain.bars import to_ns
from tradex.domain.instruments import Instrument
from tradex.news.calendar import NewsCalendar
from tradex.news.events import Impact, NewsEvent, TimePrecision
from tradex.news.providers import (
    ForexFactoryProvider,
    FredProvider,
    HolidayProvider,
    provider_by_name,
)
from tradex.news.store import NewsStore
from tradex.risk.engine import RiskEngine

MINUTE = 60_000_000_000
CPI = datetime(2025, 3, 12, 12, 30, tzinfo=UTC)  # 08:30 New York


def event(
    moment: datetime,
    name: str = "CPI m/m",
    impact: Impact = Impact.HIGH,
    country: str = "USD",
    precision: TimePrecision = TimePrecision.EXACT,
) -> NewsEvent:
    return NewsEvent(
        ts=to_ns(moment),
        name=name,
        country=country,
        impact=impact,
        source="test",
        precision=precision,
    )


def news_config(**overrides: object) -> NewsConfig:
    return NewsConfig(**{"enabled": True, **overrides})


# ------------------------------------------------------------------- Domaene
@pytest.mark.parametrize(
    ("raw", "expected"),
    [("High", Impact.HIGH), ("low", Impact.LOW), ("Moderate", Impact.MEDIUM), ("3", Impact.HIGH)],
)
def test_impact_schreibweisen(raw: str, expected: Impact):
    assert Impact.parse(raw) is expected


def test_unbekannte_impact_stufe_wird_nicht_geraten():
    """"Vermutlich mittel" waere eine erfundene Sperre."""
    with pytest.raises(ValueError, match="Unbekannte Impact-Stufe"):
        Impact.parse("vielleicht")


def test_feiertag_zaehlt_als_hoch():
    """An einem Feiertag ist der Markt duenn - genau der Zustand, der gemieden
    werden soll. Die Quelle nennt das nicht "high", gemeint ist es aber."""
    assert Impact.parse("Holiday") is Impact.HIGH


# ------------------------------------------------------------------ Speicher
def test_speicher_haelt_und_liefert_sortiert(tmp_path: Path):
    store = NewsStore(tmp_path / "events.jsonl")
    assert store.read() == ()

    spaet = event(CPI + timedelta(days=1), "NFP")
    frueh = event(CPI)
    added, total = store.merge([spaet, frueh])

    assert (added, total) == (2, 2)
    assert [e.name for e in store.read()] == ["CPI m/m", "NFP"]


def test_speicher_fuehrt_zusammen_statt_zu_ueberschreiben(tmp_path: Path):
    """Die kostenlose Quelle liefert nur die laufende Woche.

    Wuerde jeder Abruf ueberschreiben, waere der Bestand nach einem Jahr
    immer noch eine Woche gross - und jeder Backtest davor blind.
    """
    store = NewsStore(tmp_path / "events.jsonl")
    store.merge([event(CPI)])
    added, total = store.merge([event(CPI + timedelta(days=7), "NFP")])

    assert (added, total) == (1, 2)


def test_bekannter_termin_behaelt_seine_fassung(tmp_path: Path):
    """Eine gemeldete Uhrzeit darf nie durch eine geratene ersetzt werden."""
    store = NewsStore(tmp_path / "events.jsonl")
    store.merge([event(CPI, precision=TimePrecision.EXACT)])
    store.merge([event(CPI, precision=TimePrecision.ASSUMED)])

    stored = store.read()
    assert len(stored) == 1
    assert stored[0].precision is TimePrecision.EXACT


def test_kaputte_zeile_nennt_ihre_nummer(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"ts": 1, "name": "A", "country": "USD", "impact": "high"}\nkaputt\n', "utf-8")
    with pytest.raises(ValueError, match=r"events\.jsonl:2"):
        NewsStore(path).read()


# ------------------------------------------------------------------ Kalender
def test_fenster_liegt_um_den_termin():
    calendar = NewsCalendar([event(CPI)], news_config(block_before_minutes=10, block_after_minutes=5))
    ts = to_ns(CPI)

    assert calendar.blackout_at(ts - 11 * MINUTE) is None
    assert calendar.blackout_at(ts - 9 * MINUTE) is not None
    assert calendar.blackout_at(ts) is not None
    assert calendar.blackout_at(ts + 4 * MINUTE) is not None
    assert calendar.blackout_at(ts + 6 * MINUTE) is None


def test_geratene_uhrzeit_bekommt_ein_breiteres_fenster():
    """Sonst taeuscht das Fenster eine Genauigkeit vor, die die Quelle nicht hat."""
    config = news_config(
        block_before_minutes=10, block_after_minutes=10, assumed_time_extra_minutes=20
    )
    exakt = NewsCalendar([event(CPI)], config)
    geraten = NewsCalendar([event(CPI, precision=TimePrecision.ASSUMED)], config)

    weite = lambda c: c.blackouts[0].end - c.blackouts[0].start  # noqa: E731
    assert weite(geraten) == weite(exakt) + 40 * MINUTE


def test_nur_tag_bekannt_wird_je_nach_regel_ignoriert_oder_ganz_gesperrt():
    tages_termin = event(CPI, "Thanksgiving", precision=TimePrecision.DAY_ONLY)

    ignoriert = NewsCalendar([tages_termin], news_config(day_only_policy="ignore"))
    assert ignoriert.is_empty
    assert ignoriert.skipped == 1

    gesperrt = NewsCalendar([tages_termin], news_config(day_only_policy="block_day"))
    window = gesperrt.blackouts[0]
    assert window.end - window.start == 24 * 60 * MINUTE
    # Der ganze Tag, nicht 24 Stunden um den Termin herum.
    assert gesperrt.blackout_at(to_ns(CPI.replace(hour=0, minute=1))) is not None
    assert gesperrt.blackout_at(to_ns(CPI - timedelta(minutes=2))) is not None


def test_schwache_und_fremde_termine_sperren_nicht():
    events = [
        event(CPI, "Kleinkram", impact=Impact.LOW),
        event(CPI + timedelta(hours=1), "NZ CPI", country="NZD"),
    ]
    calendar = NewsCalendar(events, news_config(min_impact="high", countries=("USD",)))
    assert calendar.is_empty
    assert calendar.considered == 0


def test_ueberlappende_fenster_werden_zusammengezogen():
    """CPI und Retail Sales zur selben Minute sind EIN Sperrfenster."""
    events = [event(CPI, "CPI"), event(CPI + timedelta(minutes=5), "Retail Sales")]
    calendar = NewsCalendar(events, news_config(block_before_minutes=15, block_after_minutes=15))

    assert len(calendar.blackouts) == 1
    window = calendar.blackouts[0]
    assert window.events == ("CPI", "Retail Sales")
    assert window.label == "CPI +1"
    assert window.end - window.start == 35 * MINUTE


def test_abdeckung_kennt_ihre_grenzen():
    """Der teuerste Fehler waere ein Filter, der ausserhalb seiner Daten
    aussieht wie einer, der nichts zu beanstanden hat."""
    calendar = NewsCalendar([event(CPI)], news_config())

    assert calendar.covers(to_ns(CPI))
    assert not calendar.covers(to_ns(CPI - timedelta(days=1)))
    assert not calendar.covers(to_ns(CPI + timedelta(days=1)))
    assert not NewsCalendar([], news_config()).covers(to_ns(CPI))


def test_kalender_ist_reproduzierbar():
    """Invariante 2: zweimal dieselbe Eingabe, zweimal dasselbe Ergebnis."""
    events = [event(CPI + timedelta(hours=h), f"E{h}") for h in range(12)]
    a = NewsCalendar(events, news_config())
    b = NewsCalendar(list(reversed(events)), news_config())
    assert a.blackouts == b.blackouts


# --------------------------------------------------------------- Risk Engine
def _engine(config: Config, instrument: Instrument, calendar: NewsCalendar | None) -> RiskEngine:
    return RiskEngine(config, instrument, news=calendar)


def _with_news(config: Config, **overrides: object) -> Config:
    return Config(**{**config.model_dump(), "news": news_config(**overrides)})


def test_abgeschalteter_filter_laesst_alles_durch(config: Config, mnq: Instrument):
    result = _engine(config, mnq, None).check_news(to_ns(CPI))
    assert [r.code for r in result] == [R.NEWS_OK]
    assert all(r.ok for r in result)


def test_sperrfenster_lehnt_ab(config: Config, mnq: Instrument):
    calendar = NewsCalendar([event(CPI)], news_config())
    result = _engine(_with_news(config), mnq, calendar).check_news(to_ns(CPI))

    assert [r.code for r in result] == [R.NEWS_BLACKOUT]
    assert not result[0].ok
    assert result[0].params["event"] == "CPI m/m"


def test_ausserhalb_des_fensters_wird_gehandelt(config: Config, mnq: Instrument):
    # Zwei Termine, damit der gepruefte Zeitpunkt INNERHALB der Abdeckung
    # liegt - sonst waere die Antwort "keine Daten" und nicht "kein Termin".
    calendar = NewsCalendar([event(CPI), event(CPI + timedelta(days=1), "NFP")], news_config())
    result = _engine(_with_news(config), mnq, calendar).check_news(
        to_ns(CPI + timedelta(minutes=30))
    )
    assert [r.code for r in result] == [R.NEWS_OK]
    assert result[0].ok


@pytest.mark.parametrize(("policy", "erlaubt"), [("warn", True), ("block", False)])
def test_fehlende_termindaten_werden_gemeldet(
    config: Config, mnq: Instrument, policy: str, erlaubt: bool
):
    """Ein eingeschalteter Filter ohne Daten ist der gefaehrlichste Zustand.

    Er meldet nichts und sieht dabei aus wie einer, der nichts zu beanstanden
    hat. Deshalb ein eigener Code - und die Wahl, ob das reicht.
    """
    tuned = _with_news(config, on_missing_data=policy)
    calendar = NewsCalendar([event(CPI)], tuned.news)
    result = _engine(tuned, mnq, calendar).check_news(to_ns(CPI + timedelta(days=30)))

    assert [r.code for r in result] == [R.NEWS_NO_DATA]
    assert result[0].ok is erlaubt


def test_eingeschaltet_ohne_kalender_meldet_ebenfalls(config: Config, mnq: Instrument):
    result = _engine(_with_news(config), mnq, None).check_news(to_ns(CPI))
    assert [r.code for r in result] == [R.NEWS_NO_DATA]


# ------------------------------------------------------------------- Provider
class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_forexfactory_wird_richtig_gelesen(monkeypatch: pytest.MonkeyPatch):
    payload = (
        b'[{"title":"CPI m/m","country":"USD","date":"2025-03-12T08:30:00-04:00",'
        b'"impact":"High","forecast":"0.3%","previous":"0.5%"},'
        b'{"title":"Bank Holiday","country":"JPY","date":"2025-03-12T00:00:00-04:00",'
        b'"impact":"Holiday","forecast":"","previous":""},'
        b'{"title":"Krude","country":"USD","date":"2025-03-12T10:30:00-04:00",'
        b'"impact":"Unbekannt","forecast":"","previous":""}]'
    )
    monkeypatch.setattr("tradex.news.providers._get", lambda url: payload)

    events = list(ForexFactoryProvider().fetch(date(2025, 3, 12), date(2025, 3, 13)))

    assert [e.name for e in events] == ["CPI m/m", "Bank Holiday"], "Unbekanntes wird uebersprungen"
    assert events[0].ts == to_ns(CPI), "08:30 New York sind 12:30 UTC"
    assert events[0].precision is TimePrecision.EXACT
    assert events[1].impact is Impact.HIGH


def test_forexfactory_haelt_sich_an_den_zeitraum(monkeypatch: pytest.MonkeyPatch):
    payload = b'[{"title":"CPI","country":"USD","date":"2025-03-12T08:30:00-04:00","impact":"High"}]'
    monkeypatch.setattr("tradex.news.providers._get", lambda url: payload)
    assert list(ForexFactoryProvider().fetch(date(2025, 3, 13), date(2025, 3, 20))) == []


def test_fred_ergaenzt_die_uhrzeit_und_sagt_es_dazu(monkeypatch: pytest.MonkeyPatch):
    payload = b'{"release_dates":[{"release_id":10,"date":"2025-03-12"}]}'
    monkeypatch.setattr("tradex.news.providers._get", lambda url: payload)

    events = [
        e
        for e in FredProvider("schluessel").fetch(date(2025, 3, 1), date(2025, 4, 1))
        if e.name == "Consumer Price Index"
    ]
    assert events[0].ts == to_ns(CPI), "08:30 New York"
    assert events[0].precision is TimePrecision.ASSUMED, "die Uhrzeit ist ergaenzt, nicht gemeldet"


def test_fred_ohne_schluessel_sagt_wo_es_einen_gibt():
    with pytest.raises(ValueError, match="fredaccount"):
        FredProvider("")


def test_feiertage_brauchen_kein_netz():
    events = list(HolidayProvider().fetch(date(2025, 1, 1), date(2026, 1, 1)))
    namen = {e.name for e in events}

    assert {"Neujahr", "Karfreitag", "Thanksgiving", "Weihnachten", "Juneteenth"} <= namen
    assert all(e.precision is TimePrecision.DAY_ONLY for e in events)
    assert all(e.impact is Impact.HIGH for e in events)


@pytest.mark.parametrize(
    ("jahr", "karfreitag"),
    [(2023, date(2023, 4, 7)), (2024, date(2024, 3, 29)), (2025, date(2025, 4, 18)),
     (2026, date(2026, 4, 3)), (2027, date(2027, 3, 26))],
)
def test_osterrechnung_gegen_bekannte_daten(jahr: int, karfreitag: date):
    """Die Formel sieht willkuerlich aus - deshalb gegen Kalender geprueft."""
    events = list(HolidayProvider().fetch(date(jahr, 1, 1), date(jahr + 1, 1, 1)))
    gefunden = next(e for e in events if e.name == "Karfreitag")
    assert datetime.fromtimestamp(gefunden.ts / 1e9, tz=UTC).date() == karfreitag


def test_feiertag_am_wochenende_wird_verschoben():
    """4. Juli 2026 ist ein Samstag - der Markt schliesst am Freitag."""
    events = list(HolidayProvider().fetch(date(2026, 1, 1), date(2027, 1, 1)))
    juli = next(e for e in events if e.name == "Independence Day")
    assert datetime.fromtimestamp(juli.ts / 1e9, tz=UTC).date() == date(2026, 7, 3)


def test_unbekannte_quelle_nennt_die_bekannten():
    with pytest.raises(ValueError, match="forexfactory"):
        provider_by_name("bloomberg")


# --------------------------------------------------------- Ende zu Ende
def test_der_filter_verhindert_im_backtest_tatsaechlich_trades(
    config: Config, mnq: Instrument
):
    """Waechter: ohne diesen Test bewiese keiner der anderen etwas.

    Alle Tests darueber pruefen Bausteine. Dieser prueft, dass der Filter im
    fertig verdrahteten Backtest ankommt - und dass er dort Trades kostet.
    Ein Filter, der nirgends angeschlossen ist, besteht jede Einzelpruefung.
    """
    series = trending_market(60 * 24 * 6)
    tuned = tradeable_config(config)

    ohne = Backtester("MNQ", mnq, tuned).run(series)
    assert ohne.trades, "ohne Sperre muss es Trades geben, sonst prueft der Test nichts"

    # Jede Stunde ein Termin: das sperrt praktisch durchgehend.
    start = datetime.fromtimestamp(series[0].ts / 1e9, tz=UTC)
    dauerfeuer = [
        event(start + timedelta(hours=h), f"Termin {h}") for h in range(24 * 7)
    ]
    gesperrt = Config(**{**tuned.model_dump(), "news": news_config(block_after_minutes=60)})
    backtester = Backtester("MNQ", mnq, gesperrt)
    backtester.books["MNQ"].strategy.risk.news = NewsCalendar(dauerfeuer, gesperrt.news)

    mit = backtester.run(series)
    assert len(mit.trades) < len(ohne.trades)
    assert R.NEWS_BLACKOUT in mit.rejections


def test_der_filter_beendet_keine_positionen(config: Config, mnq: Instrument):
    """Eine offene Position muss ihren Stop erreichen duerfen.

    Geprueft wird strukturell: `tradex/news/` kennt die Ausfuehrung nicht.
    Ein Import in diese Richtung waere der erste Schritt zu einer Sperre, die
    eine Position ungeschuetzt laesst.
    """
    quelle = Path(__file__).resolve().parent.parent / "tradex" / "news"
    for datei in quelle.glob("*.py"):
        text = datei.read_text(encoding="utf-8")
        assert "backtest" not in text, f"{datei.name} kennt die Ausfuehrung"
        assert "execution" not in text, f"{datei.name} kennt die Ausfuehrung"
