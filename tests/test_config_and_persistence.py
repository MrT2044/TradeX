"""Konfiguration, Instrumente und Persistenz."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from tests.conftest import PROJECT_ROOT
from tradex.analysis import reasons
from tradex.config import Config, load_config, load_instruments
from tradex.domain.enums import Timeframe
from tradex.domain.instruments import Instrument
from tradex.persistence.db import CURRENT_SCHEMA_VERSION, connect, init_database
from tradex.persistence.decision_log import (
    DecisionLog,
    config_fingerprint,
    make_reason,
    utc_now_iso,
)
from tradex.persistence.models import DataGapRecord, DecisionRecord, SystemEvent


# ---------------------------------------------------------------- Konfiguration
def test_default_config_laedt(config: Config):
    assert config.version == 1
    assert config.data.base_timeframe is Timeframe.M1
    assert config.timeframes.all == (
        Timeframe.H4,
        Timeframe.H1,
        Timeframe.M15,
        Timeframe.M5,
        Timeframe.M1,
    )


def test_messkonfiguration_weicht_nur_beim_konto_ab():
    """`backtest_edge.yaml` darf sich NUR in `risk.account_size` unterscheiden.

    Die Variante existiert, um die Edge-Frage vom Risikobudget zu trennen -
    nicht, um nebenbei Schwellenwerte zu veraendern. Ohne diesen Test wuerden
    die beiden Dateien mit der Zeit unbemerkt auseinanderlaufen und aus der
    Messkonfiguration wuerde eine zweite Regelfassung. Genau dann waeren die
    Ergebnisse beider Laeufe nicht mehr vergleichbar.
    """
    standard = load_config(PROJECT_ROOT / "config" / "default.yaml").model_dump()
    variant = load_config(PROJECT_ROOT / "config" / "backtest_edge.yaml").model_dump()

    assert variant["risk"]["account_size"] == 25_000.0
    assert standard["risk"]["account_size"] == 10_000.0

    standard["risk"] = {**standard["risk"], "account_size": None}
    variant["risk"] = {**variant["risk"], "account_size": None}
    assert standard == variant, "die Messkonfiguration weicht in mehr als dem Konto ab"


def test_unbekannter_schluessel_wird_abgelehnt(tmp_path: Path):
    """extra="forbid": ein Tippfehler in der YAML darf nicht still ignoriert werden.

    Genau das ist Architektur-Invariante 4 - ein vertippter Schwellenwert waere
    sonst wirkungslos, ohne dass es jemand merkt.
    """
    raw = yaml.safe_load((PROJECT_ROOT / "config" / "default.yaml").read_text(encoding="utf-8"))
    raw["analysis"]["fvg"]["min_size_tick"] = 4  # Tippfehler: fehlendes "s"
    target = tmp_path / "broken.yaml"
    target.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ValidationError, match="min_size_tick"):
        load_config(target)


def test_ungueltige_werte_werden_abgelehnt(tmp_path: Path):
    raw = yaml.safe_load((PROJECT_ROOT / "config" / "default.yaml").read_text(encoding="utf-8"))
    raw["analysis"]["fvg"]["mitigation_threshold"] = 1.5  # muss in (0, 1] liegen
    target = tmp_path / "broken.yaml"
    target.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ValidationError):
        load_config(target)


def test_live_modus_erfordert_explizite_freigabe(tmp_path: Path):
    """Spec §24: Live-Trading ist standardmaessig deaktiviert und muss doppelt bestaetigt werden."""
    raw = yaml.safe_load((PROJECT_ROOT / "config" / "default.yaml").read_text(encoding="utf-8"))
    raw["execution"]["mode"] = "live_auto"
    target = tmp_path / "live.yaml"
    target.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ValidationError, match="live_trading_enabled"):
        load_config(target)


def test_basis_timeframe_muss_teiler_sein(tmp_path: Path):
    raw = yaml.safe_load((PROJECT_ROOT / "config" / "default.yaml").read_text(encoding="utf-8"))
    raw["data"]["base_timeframe"] = "15m"
    target = tmp_path / "broken.yaml"
    target.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ValidationError, match="Aggregation"):
        load_config(target)


def test_instrument_kontraktspezifikation(mnq: Instrument, instruments: dict[str, Instrument]):
    """Gegen die CME-Kontraktspezifikation geprueft, nicht angenommen."""
    assert (mnq.tick_size, mnq.tick_value, mnq.point_value) == (0.25, 0.50, 2.00)
    nq = instruments["NQ"]
    assert (nq.tick_size, nq.tick_value, nq.point_value) == (0.25, 5.00, 20.00)
    assert nq.point_value == mnq.point_value * 10, "NQ ist das Zehnfache von MNQ"


def test_inkonsistente_kontraktspezifikation_wird_abgelehnt(tmp_path: Path):
    """tick_value muss aus tick_size * point_value folgen - sonst rechnet das Risk-Modul falsch."""
    raw = yaml.safe_load(
        (PROJECT_ROOT / "config" / "instruments.yaml").read_text(encoding="utf-8")
    )
    raw["instruments"]["MNQ"]["tick_value"] = 0.75
    target = tmp_path / "broken.yaml"
    target.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="tick_value"):
        load_instruments(target)


def test_preisumrechnung(mnq: Instrument):
    assert mnq.to_ticks(2.5) == 10.0
    assert mnq.to_points(10) == 2.5
    assert mnq.points_to_currency(10.0) == 20.0
    assert mnq.points_to_currency(10.0, quantity=3) == 60.0
    assert mnq.round_to_tick(21345.13) == 21345.25
    assert mnq.round_to_tick(21345.62) == 21345.50


# --------------------------------------------------------------------- Reasons
def test_alle_reason_codes_sind_registriert():
    """Jeder Code im Modul muss in ALL_CODES stehen - sonst fehlt ihm die Uebersetzung."""
    declared = {
        value
        for name, value in vars(reasons).items()
        if name.isupper() and name != "ALL_CODES" and isinstance(value, str)
    }
    assert declared == set(reasons.ALL_CODES)


def test_jeder_reason_code_hat_eine_deutsche_uebersetzung():
    """Die Engine liefert Codes, das UI uebersetzt sie - beide muessen zusammenpassen.

    Ohne diese Pruefung faellt ein fehlender Code erst auf, wenn im Dashboard
    ein roher Bezeichner oder "undefined" steht.
    """
    translations = (PROJECT_ROOT / "ui" / "src" / "i18n" / "de.ts").read_text(encoding="utf-8")
    missing = [code for code in reasons.ALL_CODES if f"'{code}'" not in translations]
    assert not missing, f"Ohne deutsche Uebersetzung: {missing}"


def test_ablehnungscodes_haben_eine_kurzbezeichnung():
    """Die Zaehlansicht hat keine Parameter - dort braucht es parameterfreie Labels.

    Genau hier stand zuvor "undefined" im Dashboard, weil die ausformulierten
    Saetze ohne Parameter uebersetzt wurden.
    """
    translations = (PROJECT_ROOT / "ui" / "src" / "i18n" / "de.ts").read_text(encoding="utf-8")
    label_block = translations.split("export const reasonLabel", 1)[-1].split("};", 1)[0]

    rejection_codes = [
        reasons.SETUP_INVALIDATED_BEYOND_SWEEP,
        reasons.SETUP_INVALIDATED_BIAS_FLIP,
        reasons.SETUP_EXPIRED,
        reasons.MSS_MISSING,
        reasons.TARGET_RR_TOO_LOW,
        reasons.TARGET_NONE,
        reasons.STOP_TOO_WIDE,
        reasons.STOP_TOO_TIGHT,
        reasons.RISK_SIZE_ZERO,
        reasons.RISK_DAILY_LOSS_LIMIT,
        reasons.RISK_MAX_TRADES,
        reasons.RISK_MAX_POSITIONS,
        reasons.WINDOW_SESSION_BLOCKED,
        reasons.WINDOW_VOLATILITY_LOW,
        reasons.WINDOW_VOLATILITY_HIGH,
        reasons.DECISION_NO_TRADE,
    ]
    missing = [code for code in rejection_codes if f"'{code}'" not in label_block]
    assert not missing, f"Ohne Kurzbezeichnung fuer die Zaehlansicht: {missing}"


# ------------------------------------------------------------------ Persistenz
def test_migration_legt_schema_an(tmp_path: Path):
    database = tmp_path / "tradex.db"
    init_database(database)

    with connect(database) as conn:
        version = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"]
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

    assert version == CURRENT_SCHEMA_VERSION
    assert {
        "system_events",
        "decision_log",
        "config_snapshots",
        "strategy_versions",
        "data_gaps",
    } <= tables


def test_migration_ist_idempotent(tmp_path: Path):
    database = tmp_path / "tradex.db"
    init_database(database)
    init_database(database)  # darf nicht scheitern

    with connect(database) as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()["n"]
    assert count == CURRENT_SCHEMA_VERSION


def test_config_fingerprint_ist_stabil():
    """Zeilenenden duerfen den Hash nicht aendern - sonst waere er unter Windows anders."""
    path = PROJECT_ROOT / "config" / "default.yaml"
    first, content = config_fingerprint(path)
    second, _ = config_fingerprint(path)
    assert first == second
    assert len(first) == 16
    assert "\r" not in content


def test_entscheidungsprotokoll_speichert_no_trade(tmp_path: Path):
    """Spec §25: Auch Nicht-Entscheidungen muessen nachvollziehbar sein.

    Die Frage "warum wurde dieser Trade NICHT gemacht?" ist ohne diese
    Eintraege spaeter nicht mehr zu beantworten.
    """
    database = tmp_path / "tradex.db"
    init_database(database)

    with DecisionLog(database) as log:
        config_hash = log.register_config(PROJECT_ROOT / "config" / "default.yaml")
        log.record(
            DecisionRecord(
                ts_utc=utc_now_iso(),
                bar_ts=1_700_000_000_000_000_000,
                symbol="MNQ",
                timeframe="1m",
                decision="NO_TRADE",
                config_hash=config_hash,
                htf_bias="bullish",
                liquidity_sweep=True,
                displacement=True,
                fvg=True,
                retracement=True,
                mss=False,  # die fehlende Pflichtbedingung
                reasons=(
                    make_reason(reasons.MSS_MISSING, False, timeframe="1m", lookback_bars=10),
                ),
                context={"note": "Setup unvollstaendig"},
            )
        )

        entries = log.recent()
        assert len(entries) == 1
        entry = entries[0]
        assert entry["decision"] == "NO_TRADE"
        assert entry["mss"] is False
        assert entry["liquidity_sweep"] is True
        assert entry["reasons"][0]["code"] == reasons.MSS_MISSING
        assert entry["context"]["note"] == "Setup unvollstaendig"
        assert entry["config_hash"] == config_hash


def test_config_snapshot_wird_einmalig_gespeichert(tmp_path: Path):
    database = tmp_path / "tradex.db"
    init_database(database)
    path = PROJECT_ROOT / "config" / "default.yaml"

    with DecisionLog(database) as log:
        first = log.register_config(path)
        second = log.register_config(path)
        assert first == second

    with connect(database) as conn:
        rows = conn.execute("SELECT config_hash, content FROM config_snapshots").fetchall()
    assert len(rows) == 1
    assert "min_size_ticks" in rows[0]["content"]


def test_batch_insert(tmp_path: Path):
    database = tmp_path / "tradex.db"
    init_database(database)

    with DecisionLog(database) as log:
        config_hash = log.register_config(PROJECT_ROOT / "config" / "default.yaml")
        records = [
            DecisionRecord(
                ts_utc=utc_now_iso(),
                bar_ts=i,
                symbol="MNQ",
                timeframe="1m",
                decision="ANALYSIS",
                config_hash=config_hash,
            )
            for i in range(500)
        ]
        assert log.record_many(records) == 500
        assert log.count() == 500


def test_systemereignisse_und_datenluecken(tmp_path: Path):
    database = tmp_path / "tradex.db"
    init_database(database)

    with DecisionLog(database) as log:
        log.event(
            SystemEvent(
                ts_utc=utc_now_iso(),
                level="WARNING",
                category="data_feed",
                message="Verbindung verloren",
                payload={"provider": "replay"},
            )
        )
        gap = DataGapRecord("MNQ", "1m", 1000, 2000, 16, utc_now_iso())
        log.record_gap(gap)
        log.record_gap(gap)  # doppelte Meldung darf keinen zweiten Eintrag erzeugen

        gaps = log.gaps("MNQ", "1m")
        assert len(gaps) == 1
        assert gaps[0]["missing_bars"] == 16

    with connect(database) as conn:
        row = conn.execute("SELECT * FROM system_events").fetchone()
    assert row["category"] == "data_feed"
    assert json.loads(row["payload"])["provider"] == "replay"
