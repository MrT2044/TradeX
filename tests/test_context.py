"""MarketContext - der einzige Analysepfad.

Hier wird die Eigenschaft geprueft, ohne die ein Backtest wertlos waere:
**Determinismus**. Derselbe Input muss byte-identisch denselben Output erzeugen,
und es darf keinen Unterschied machen, ob die Daten am Stueck oder Bar fuer Bar
hereinkommen - denn genau das ist der Unterschied zwischen Backtest und Live.
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest

from tests.conftest import DEFAULT_START
from tradex.analysis.context import MarketContext
from tradex.api.schemas import ContextSnapshotDto
from tradex.config import Config
from tradex.domain.bars import BarSeries, to_ns
from tradex.domain.enums import Timeframe
from tradex.domain.instruments import Instrument


def _market_data(minutes: int, seed: int = 11) -> BarSeries:
    """Realistischere Reihe: Trend, Umkehr und Volatilitaetsschuebe.

    Rein zufaellige Daten wuerden kaum Sweeps oder MSS erzeugen; hier soll die
    Analyse tatsaechlich etwas zu tun bekommen.
    """
    rng = np.random.default_rng(seed)
    series = BarSeries()
    price = 21000.0
    for i in range(minutes):
        drift = 0.06 if i < minutes // 2 else -0.06
        shock = 6.0 if i % 350 == 0 else 1.4
        close = price + drift + float(rng.normal(0, shock))
        high = max(price, close) + abs(float(rng.normal(0, shock * 0.4)))
        low = min(price, close) - abs(float(rng.normal(0, shock * 0.4)))
        series.append(
            to_ns(DEFAULT_START + timedelta(minutes=i)),
            price,
            high,
            low,
            close,
            float(rng.integers(40, 900)),
        )
        price = close
    return series


def _snapshot_json(context: MarketContext) -> str:
    return ContextSnapshotDto.of(context.snapshot()).model_dump_json()


def test_zwei_laeufe_liefern_identische_snapshots(config: Config, mnq: Instrument):
    """Determinismus - die Grundvoraussetzung fuer jede Backtest-Aussage."""
    series = _market_data(60 * 24 * 4)

    first = MarketContext("MNQ", mnq, config)
    first.feed(series)
    second = MarketContext("MNQ", mnq, config)
    second.feed(series)

    assert _snapshot_json(first) == _snapshot_json(second)


def test_bar_fuer_bar_entspricht_stueckweisem_einspeisen(config: Config, mnq: Instrument):
    """Backtest (`feed`) und Live (`on_base_bar`) muessen denselben Zustand ergeben.

    Das ist Architektur-Invariante 3 in Testform: es gibt keinen zweiten Pfad.
    """
    series = _market_data(60 * 24 * 3)

    bulk = MarketContext("MNQ", mnq, config)
    bulk.feed(series)

    streamed = MarketContext("MNQ", mnq, config)
    for bar in series:
        streamed.on_base_bar(bar)

    assert _snapshot_json(bulk) == _snapshot_json(streamed)


def test_analyse_findet_tatsaechlich_muster(config: Config, mnq: Instrument):
    """Absicherung gegen einen Detektor, der schlicht nie etwas meldet.

    Ohne diesen Test wuerden alle Determinismus-Tests auch dann gruen bleiben,
    wenn die Analyse durchgehend leer liefert.
    """
    context = MarketContext("MNQ", mnq, config)
    updates = context.feed(_market_data(60 * 24 * 5))

    assert sum(len(u.new_swings) for u in updates) > 0, "keine Swings gefunden"
    assert sum(len(u.new_fvgs) for u in updates) > 0, "keine FVGs gefunden"
    assert sum(1 for u in updates if u.displacement) > 0, "keine Displacements gefunden"
    assert sum(1 for u in updates if u.structure_event) > 0, "keine Strukturbrueche gefunden"
    assert sum(len(u.sweeps) for u in updates) > 0, "keine Sweeps gefunden"


def test_alle_timeframes_werden_aufgebaut(config: Config, mnq: Instrument):
    context = MarketContext("MNQ", mnq, config)
    context.feed(_market_data(60 * 24 * 3))

    for timeframe in config.timeframes.all:
        assert len(context.series(timeframe)) > 0, timeframe

    assert len(context.series(Timeframe.M1)) > len(context.series(Timeframe.H4))


def test_snapshot_ist_gueltiges_json(config: Config, mnq: Instrument):
    """NaN und Inf sind kein gueltiges JSON - der ATR ist waehrend der Aufwaermphase NaN."""
    import json

    context = MarketContext("MNQ", mnq, config)
    context.feed(_market_data(200))
    payload = json.loads(_snapshot_json(context))

    assert payload["symbol"] == "MNQ"
    assert "4h" in payload["timeframes"]
    assert payload["bias"]["bias"] in ("bullish", "bearish", "neutral")


def test_bias_bleibt_neutral_ohne_ausreichende_historie(config: Config, mnq: Instrument):
    """Ohne aufgewaermte Timeframes darf keine Richtungsaussage entstehen."""
    context = MarketContext("MNQ", mnq, config)
    context.feed(_market_data(30))
    assert context.bias().bias.value == "neutral"


def test_forming_bar_ist_nicht_teil_der_serie(config: Config, mnq: Instrument):
    """Architektur-Invariante 1: die laufende Bar erreicht keinen Detektor."""
    context = MarketContext("MNQ", mnq, config)
    context.feed(_market_data(127))

    forming = context.forming(Timeframe.H1)
    assert forming is not None
    assert forming.ts not in set(context.series(Timeframe.H1).ts.tolist())


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_determinismus_unabhaengig_von_den_daten(config: Config, mnq: Instrument, seed: int):
    series = _market_data(60 * 24 * 2, seed=seed)
    a = MarketContext("MNQ", mnq, config)
    a.feed(series)
    b = MarketContext("MNQ", mnq, config)
    b.feed(series)
    assert _snapshot_json(a) == _snapshot_json(b)
