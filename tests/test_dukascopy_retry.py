"""Wiederholungsverhalten des Dukascopy-Abrufs.

Der Feed ist kostenlos und drosselt bei zu dichten Anfragen. Beim ersten
Massendownload kam nach kurzer Zeit HTTP 503 - ausgeloest durch zwei parallel
laufende Prozesse. Daraus folgen zwei Regeln, die hier abgesichert werden:

    1. 429/503 heisst "zu schnell", nicht "kaputt". Darauf gehoert eine
       deutlich laengere Pause als auf einen gewoehnlichen Netzfehler.
    2. Ein einzelner unerreichbarer Tag darf nicht den ganzen Lauf verwerfen.
"""

from __future__ import annotations

import urllib.error
from datetime import UTC, datetime

import pytest

from tradex.data import dukascopy_provider as dp
from tradex.data.provider import ProviderError

DAY = datetime(2025, 3, 19, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_real_waiting(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Wartezeiten aufzeichnen statt tatsaechlich zu warten."""
    waits: list[float] = []
    monkeypatch.setattr(dp.time, "sleep", lambda s: waits.append(s))
    monkeypatch.setattr(dp, "_last_request_at", 0.0)
    return waits


def _fail_with(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> dict[str, int]:
    calls = {"n": 0}

    def boom(*_args, **_kwargs):
        calls["n"] += 1
        raise exc

    monkeypatch.setattr("urllib.request.urlopen", boom)
    return calls


@pytest.mark.parametrize("status", [429, 503])
def test_drosselung_wartet_deutlich_laenger(
    monkeypatch: pytest.MonkeyPatch, _no_real_waiting: list[float], status: int
):
    """Nach 429/503 muss die Pause laenger sein als der normale Backoff.

    Sonst haemmert der Abruf weiter und verlaengert die Sperre nur.
    """
    _fail_with(
        monkeypatch, urllib.error.HTTPError("url", status, "throttled", None, None)
    )

    with pytest.raises(ProviderError):
        dp.fetch_day("USATECHIDXUSD", DAY)

    throttle_waits = [w for w in _no_real_waiting if w >= dp._THROTTLE_BACKOFF_SECONDS]
    assert throttle_waits, f"HTTP {status} muss eine lange Pause ausloesen"
    # Die Drosselpause muss laenger sein als der laengste normale Backoff -
    # sonst waere die Sonderbehandlung wirkungslos.
    longest_normal_backoff = dp._BACKOFF_SECONDS * 2 ** (dp._MAX_ATTEMPTS - 1)
    assert max(_no_real_waiting) > longest_normal_backoff


def test_gewoehnlicher_netzfehler_wartet_kurz(
    monkeypatch: pytest.MonkeyPatch, _no_real_waiting: list[float]
):
    """Ein Timeout ist keine Drosselung - hier genuegt der kurze Backoff."""
    _fail_with(monkeypatch, TimeoutError("read timed out"))

    with pytest.raises(ProviderError):
        dp.fetch_day("USATECHIDXUSD", DAY)

    assert _no_real_waiting
    assert max(_no_real_waiting) < dp._THROTTLE_BACKOFF_SECONDS


def test_erfolg_nach_zwischenzeitlichem_fehler(
    monkeypatch: pytest.MonkeyPatch, _no_real_waiting: list[float]
):
    """Ein voruebergehender Fehler darf den Tag nicht verlieren."""
    calls = {"n": 0}

    class _Response:
        def read(self) -> bytes:
            return b"nutzdaten"

        def __enter__(self):
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    def flaky(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("read timed out")
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", flaky)
    assert dp.fetch_day("USATECHIDXUSD", DAY) == b"nutzdaten"
    assert calls["n"] == 3


def test_404_wird_nicht_wiederholt(
    monkeypatch: pytest.MonkeyPatch, _no_real_waiting: list[float]
):
    """Tage ohne Daten sind der Normalfall - dafuer darf nicht gewartet werden."""
    calls = _fail_with(
        monkeypatch, urllib.error.HTTPError("url", 404, "Not Found", None, None)
    )

    assert dp.fetch_day("USATECHIDXUSD", DAY) == b""
    assert calls["n"] == 1
    assert _no_real_waiting == []


def test_abstand_zwischen_anfragen_wird_eingehalten(
    monkeypatch: pytest.MonkeyPatch, _no_real_waiting: list[float]
):
    """Ohne Mindestabstand brach der Abruf im Praxistest am zweiten Tag ab."""
    monkeypatch.setattr(dp.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(dp, "_last_request_at", 0.0)
    dp._throttle()
    assert _no_real_waiting, "es muss ein Mindestabstand eingehalten werden"
    assert _no_real_waiting[0] <= dp._REQUEST_DELAY_SECONDS
