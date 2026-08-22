"""Woher Termine kommen. Wird NUR vom Abrufskript benutzt, nie von der Engine.

Die Trennung ist dieselbe wie bei Kursdaten: ein Skript holt, ein Speicher
haelt, die Engine liest. Kein Provider wird je waehrend einer Entscheidung
befragt (Invariante 2).

Die Lage bei kostenlosen Quellen
--------------------------------
Es gibt keine kostenlose Quelle, die beides kann - Historie UND genaue
Uhrzeit. Deshalb zwei:

    ForexFactory   genaue Uhrzeit, Impact-Stufe, KEIN Schluessel
                   -> aber nur die laufende Woche
    FRED           Historie bis in die 1990er, freier Schluessel ohne
                   Kreditkarte -> aber nur der TAG, keine Uhrzeit

Beide schreiben in denselben Speicher. Der Bestand fuer den laufenden Betrieb
waechst woechentlich aus ForexFactory; die Historie fuer Backtests kommt aus
FRED und traegt deshalb `TimePrecision.ASSUMED` oder `DAY_ONLY` - was der
Kalender in breiteren Fenstern beruecksichtigt, statt eine Genauigkeit
vorzutaeuschen, die die Quelle nicht hat.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from tradex.news.events import Impact, NewsEvent, TimePrecision

USER_AGENT = "TradeX/0.1 (privates Forschungsprojekt)"
TIMEOUT_SECONDS = 30

#: Boersenzeit der US-Veroeffentlichungen. Steht hier und nicht in der Config,
#: weil es keine Einstellung ist, sondern eine Eigenschaft der Behoerden: das
#: BLS veroeffentlicht seit Jahrzehnten um 08:30 Ortszeit New York.
_US_EASTERN = ZoneInfo("America/New_York")


class NewsProvider(Protocol):
    """Was jede Quelle koennen muss."""

    name: str

    def fetch(self, start: date, end: date) -> Iterator[NewsEvent]:
        """Termine im Zeitraum [start, end). Darf weniger liefern, nie mehr."""
        ...


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return response.read()


# ------------------------------------------------------------- ForexFactory
class ForexFactoryProvider:
    """Wirtschaftskalender der laufenden Woche, ohne Anmeldung.

    Liefert exakte Zeitstempel mit Zeitzone und eine Impact-Stufe - genau das,
    was der Filter braucht. Der Haken steht im Modul-Docstring: nur die
    laufende Woche. Fuer den Live-Betrieb reicht das (woechentlich abrufen),
    fuer Backtests ueber Jahre nicht.
    """

    name = "forexfactory"
    URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

    def fetch(self, start: date, end: date) -> Iterator[NewsEvent]:
        raw = json.loads(_get(self.URL))
        for entry in raw:
            try:
                moment = datetime.fromisoformat(str(entry["date"]))
                impact = Impact.parse(str(entry["impact"]))
            except (KeyError, ValueError):
                # Eine unbekannte Impact-Stufe wird uebersprungen, nicht
                # geraten: "vermutlich mittel" waere eine erfundene Sperre.
                continue
            if not start <= moment.astimezone(UTC).date() < end:
                continue
            yield NewsEvent(
                ts=int(moment.astimezone(UTC).timestamp() * 1_000_000_000),
                name=str(entry.get("title", "")).strip(),
                country=str(entry.get("country", "")).strip().upper(),
                impact=impact,
                source=self.name,
                precision=TimePrecision.EXACT,
            )


# --------------------------------------------------------------------- FRED
@dataclass(frozen=True, slots=True)
class FredRelease:
    """Eine FRED-Veroeffentlichungsreihe und ihre uebliche Uhrzeit."""

    release_id: int
    name: str
    publish_time: time
    impact: Impact


#: Die Veroeffentlichungen, die den Nasdaq tatsaechlich bewegen. Bewusst eine
#: kurze, feste Liste statt "alles, was FRED kennt": FRED fuehrt ueber 300
#: Releases, die allermeisten davon bewegen keinen Index-Future - und jede
#: zusaetzliche Sperre kostet Handelszeit, ohne Risiko zu senken.
FRED_RELEASES: tuple[FredRelease, ...] = (
    FredRelease(10, "Consumer Price Index", time(8, 30), Impact.HIGH),
    FredRelease(50, "Employment Situation", time(8, 30), Impact.HIGH),
    FredRelease(46, "Producer Price Index", time(8, 30), Impact.MEDIUM),
    FredRelease(53, "Gross Domestic Product", time(8, 30), Impact.MEDIUM),
    FredRelease(54, "Personal Income and Outlays", time(8, 30), Impact.HIGH),
    FredRelease(9, "Advance Retail Sales", time(8, 30), Impact.MEDIUM),
    FredRelease(101, "FOMC Press Release", time(14, 0), Impact.HIGH),
)


class FredProvider:
    """Historische Veroeffentlichungstermine der US-Statistikbehoerden.

    Braucht einen kostenlosen API-Schluessel (fred.stlouisfed.org, ohne
    Kreditkarte). Liefert je Release die Tage, an denen veroeffentlicht wurde -
    zurueck bis in die 1990er. Die UHRZEIT liefert FRED historisch nicht; sie
    wird aus `FRED_RELEASES` ergaenzt und deshalb als `ASSUMED` markiert. Der
    Kalender rechnet solchen Terminen ein breiteres Fenster zu.

    Das ist eine Annahme, und sie steht ausdruecklich im Datenbestand - nicht
    als stille Genauigkeit, die niemand mehr hinterfragt.
    """

    name = "fred"
    BASE = "https://api.stlouisfed.org/fred/release/dates"

    def __init__(self, api_key: str, releases: tuple[FredRelease, ...] = FRED_RELEASES) -> None:
        if not api_key:
            raise ValueError(
                "FRED braucht einen Schluessel. Kostenlos unter "
                "https://fredaccount.stlouisfed.org/apikeys - dann in .env als "
                "FRED_API_KEY hinterlegen."
            )
        self.api_key = api_key
        self.releases = releases

    def fetch(self, start: date, end: date) -> Iterator[NewsEvent]:
        for release in self.releases:
            for day in self._dates(release, start, end):
                local = datetime.combine(day, release.publish_time, tzinfo=_US_EASTERN)
                yield NewsEvent(
                    ts=int(local.astimezone(UTC).timestamp() * 1_000_000_000),
                    name=release.name,
                    country="USD",
                    impact=release.impact,
                    source=self.name,
                    precision=TimePrecision.ASSUMED,
                )

    def _dates(self, release: FredRelease, start: date, end: date) -> list[date]:
        url = (
            f"{self.BASE}?release_id={release.release_id}&api_key={self.api_key}"
            f"&file_type=json&realtime_start={start:%Y-%m-%d}&realtime_end={end:%Y-%m-%d}"
            f"&include_release_dates_with_no_data=false&limit=10000"
        )
        try:
            payload = json.loads(_get(url))
        except urllib.error.HTTPError as error:
            raise RuntimeError(
                f"FRED lehnt die Anfrage ab ({error.code}). Bei 400 stimmt meist der "
                f"Schluessel nicht, bei 429 war es zu schnell. Release {release.release_id}."
            ) from error
        found = []
        for entry in payload.get("release_dates", []):
            day = date.fromisoformat(entry["date"])
            if start <= day < end:
                found.append(day)
        return found


# ------------------------------------------------------------------ Feiertage
class HolidayProvider:
    """US-Boersenfeiertage und verkuerzte Tage - ohne jede Netzabfrage.

    Warum als Provider und nicht als Instrument-Override: an einem halben
    Handelstag ist der Markt duenn, und das ist ein Zustand, den der
    Nachrichtenfilter meiden soll - nicht eine Frage der Handelszeiten. Die
    Regeln sind Kalenderarithmetik und aendern sich nicht, deshalb braucht es
    dafuer keine API.

    Bewusst NUR die Tage, an denen der Aktienmarkt frueher schliesst oder gar
    nicht handelt und die Futures duenn laufen.
    """

    name = "holidays"

    def fetch(self, start: date, end: date) -> Iterator[NewsEvent]:
        for year in range(start.year, end.year + 1):
            for day, label in _us_market_holidays(year):
                if not start <= day < end:
                    continue
                # Mittag Ortszeit New York als Ankerpunkt; gesperrt wird
                # ohnehin der ganze Tag (DAY_ONLY).
                local = datetime.combine(day, time(12, 0), tzinfo=_US_EASTERN)
                yield NewsEvent(
                    ts=int(local.astimezone(UTC).timestamp() * 1_000_000_000),
                    name=label,
                    country="USD",
                    impact=Impact.HIGH,
                    source=self.name,
                    precision=TimePrecision.DAY_ONLY,
                )


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    """Der n-te <weekday> eines Monats (Montag = 0)."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (nth - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Der letzte <weekday> eines Monats."""
    day = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return day - timedelta(days=(day.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    """Feste Datumsfeiertage verschieben sich aufs Wochenende hin oder weg."""
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _good_friday(year: int) -> date:
    """Karfreitag - der einzige bewegliche Feiertag, der hier zaehlt.

    Osterberechnung nach dem anonymen gregorianischen Algorithmus. Sie sieht
    willkuerlich aus, ist aber die Standardform; ein Test prueft sie gegen
    bekannte Osterdaten.
    """
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    lam = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lam) // 451
    month, day = divmod(h + lam - 7 * m + 114, 31)
    return date(year, month, day + 1) - timedelta(days=2)


def _us_market_holidays(year: int) -> list[tuple[date, str]]:
    return [
        (_observed(date(year, 1, 1)), "Neujahr"),
        (_nth_weekday(year, 1, 0, 3), "Martin Luther King Day"),
        (_nth_weekday(year, 2, 0, 3), "Presidents Day"),
        (_good_friday(year), "Karfreitag"),
        (_last_weekday(year, 5, 0), "Memorial Day"),
        (_observed(date(year, 6, 19)), "Juneteenth"),
        (_observed(date(year, 7, 4)), "Independence Day"),
        (_nth_weekday(year, 9, 0, 1), "Labor Day"),
        (_nth_weekday(year, 11, 3, 4), "Thanksgiving"),
        (_observed(date(year, 12, 25)), "Weihnachten"),
    ]


def provider_by_name(name: str, api_key: str = "") -> NewsProvider:
    """Quelle nach Name - der einzige Ort, an dem Provider entstehen."""
    if name == ForexFactoryProvider.name:
        return ForexFactoryProvider()
    if name == FredProvider.name:
        return FredProvider(api_key)
    if name == HolidayProvider.name:
        return HolidayProvider()
    known = ", ".join((ForexFactoryProvider.name, FredProvider.name, HolidayProvider.name))
    raise ValueError(f"Unbekannte Nachrichtenquelle {name!r}. Bekannt: {known}")
