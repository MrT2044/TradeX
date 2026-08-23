"""Risk Engine - die letzte Instanz vor einem Trade (Spec §10, §24).

Reihenfolge der Pruefungen ist bewusst gewaehlt: erst die harten Sperren
(Tagesverlust, Trade-Anzahl, offene Positionen), dann Marktbedingungen
(Handelsfenster, Volatilitaet, Spread), zuletzt die Positionsgroesse. So steht
im Protokoll immer der ERSTE und damit wichtigste Ablehnungsgrund vorn - bei
erreichtem Tagesverlustlimit ist es unerheblich, ob das CRV gepasst haette.

Die Risk Engine kann nur ablehnen, nie etwas erlauben, was die Strategie nicht
vorgesehen hat. Sie ist ein Filter, kein Ausloeser.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from tradex.analysis import reasons
from tradex.config import Config
from tradex.domain.enums import SessionName
from tradex.domain.instruments import Instrument
from tradex.news.calendar import NewsCalendar
from tradex.persistence.models import Reason
from tradex.risk.ledger import RiskLedger
from tradex.risk.sizing import PositionSize, calculate_position_size


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    """Ergebnis der Risikopruefung."""

    approved: bool
    position: PositionSize | None
    reasons: tuple[Reason, ...]

    @property
    def blocking_reason(self) -> str:
        for reason in self.reasons:
            if not reason.ok:
                return reason.code
        return ""


class RiskEngine:
    """Prueft ein fertiges Setup gegen alle Risikogrenzen."""

    def __init__(
        self,
        config: Config,
        instrument: Instrument,
        ledger: RiskLedger | None = None,
        news: NewsCalendar | None = None,
    ) -> None:
        self.config = config
        self.instrument = instrument
        self.ledger = ledger or RiskLedger()
        # Der Kalender wird von aussen hereingereicht, nicht hier geladen: die
        # Risk Engine darf keine Datei anfassen (Invariante 2). Wer sie baut,
        # entscheidet auch, welchen Datenbestand sie sieht - und Backtest wie
        # Live bekommen so nachweislich denselben.
        self.news = news
        self.halt_reason = ""
        """Not-Aus des laufenden Betriebs (Spec Paragraph 24). Solange gesetzt,
        entsteht keine neue Position - offene laufen weiter zu ihrem Stop.

        Bewusst hier und nicht in der Sitzung: eine Sperre, die dadurch wirkt,
        dass keine Bars mehr eingespeist werden, laesst offene Positionen ohne
        Stopueberwachung zurueck. Im Backtest ist das Feld immer leer."""

    # ----------------------------------------------------------- Nachrichten
    def check_news(self, ts: int) -> list[Reason]:
        """Spec Paragraph 14/15: kein EINSTIEG in einem Sperrfenster.

        Ausstiege sind hier nie betroffen - diese Methode wird ausschliesslich
        beim Pruefen eines Vorschlags aufgerufen, nie beim Beenden einer
        Position.
        """
        params = self.config.news
        if not params.enabled:
            return [Reason(reasons.NEWS_OK, True, {"enabled": False})]
        if self.news is None or self.news.is_empty:
            # Eingeschaltet, aber ohne Daten. Das ist der gefaehrlichste
            # Zustand ueberhaupt: der Filter meldet nichts und sieht dabei aus
            # wie einer, der nichts zu beanstanden hat.
            blocked = params.on_missing_data == "block"
            return [Reason(reasons.NEWS_NO_DATA, not blocked, {"reason": "kein Kalender geladen"})]

        if not self.news.covers(ts):
            blocked = params.on_missing_data == "block"
            return [
                Reason(
                    reasons.NEWS_NO_DATA,
                    not blocked,
                    {"ts": ts, "von": self.news.first_ts, "bis": self.news.last_ts},
                )
            ]

        window = self.news.blackout_at(ts)
        if window is None:
            return [Reason(reasons.NEWS_OK, True, {"events": self.news.considered})]
        return [
            Reason(
                reasons.NEWS_BLACKOUT,
                False,
                {
                    "event": window.label,
                    "impact": window.impact.value,
                    "minuten_bis_ende": round((window.end - ts) / 60e9, 1),
                },
            )
        ]

    # --------------------------------------------------------------- Marktfilter
    def check_trading_window(self, session: str, atr: float) -> list[Reason]:
        """Spec §13: Filter fuer Marktbedingungen - ausdruecklich kein Ausloeser."""
        params = self.config.trading_windows
        if not params.enabled:
            return [Reason(reasons.WINDOW_OK, True, {"enabled": False})]

        collected: list[Reason] = []

        allowed = {s.value for s in params.sessions}
        session_ok = session in allowed
        collected.append(
            Reason(
                reasons.WINDOW_SESSION_BLOCKED if not session_ok else reasons.WINDOW_OK,
                session_ok,
                {"session": session, "allowed": sorted(allowed)},
            )
        )

        if np.isfinite(atr):
            atr_ticks = self.instrument.to_ticks(atr)
            if atr_ticks < params.min_atr_ticks:
                collected.append(
                    Reason(
                        reasons.WINDOW_VOLATILITY_LOW,
                        False,
                        {"atr_ticks": round(atr_ticks, 1), "min": params.min_atr_ticks},
                    )
                )
            elif atr_ticks > params.max_atr_ticks:
                collected.append(
                    Reason(
                        reasons.WINDOW_VOLATILITY_HIGH,
                        False,
                        {"atr_ticks": round(atr_ticks, 1), "max": params.max_atr_ticks},
                    )
                )
        return collected

    # ------------------------------------------------------------------ Grenzen
    def check_limits(self, trading_day: int, pending: int = 0, ts: int = 0) -> list[Reason]:
        """Harte Sperren pruefen.

        `pending` sind Trades, die auf DERSELBEN Bar bereits genehmigt wurden,
        aber noch in keinem Buch stehen. Ohne sie waere `max_open_positions`
        wirkungslos, sobald mehrere Setups gleichzeitig bestaetigen: alle
        saehen dieselbe leere Position und kaemen alle durch (Spec §24,
        Duplicate Order Protection).
        """
        # Der Not-Aus steht VOR allem anderen, auch vor `risk.enabled`: eine
        # abgeschaltete Risikopruefung ist eine Testbedingung, ein angehaltener
        # Betrieb ist ein Zustand der Wirklichkeit.
        if self.halt_reason:
            return [Reason(reasons.SYSTEM_HALTED, False, {"grund": self.halt_reason})]

        params = self.config.risk
        if not params.enabled:
            return [Reason(reasons.RISK_DISABLED, True, {})]

        day = self.ledger.day(trading_day)
        collected: list[Reason] = []

        loss = day.realized_loss
        limit = params.max_daily_loss_amount
        within_loss = loss < limit
        collected.append(
            Reason(
                reasons.RISK_DAILY_LOSS_LIMIT,
                within_loss,
                {"realized_loss": round(loss, 2), "limit": round(limit, 2)},
            )
        )

        taken = day.trades_taken + pending
        within_trades = taken < params.max_trades_per_day
        collected.append(
            Reason(
                reasons.RISK_MAX_TRADES,
                within_trades,
                {"taken": taken, "limit": params.max_trades_per_day, "pending": pending},
            )
        )

        open_count = self.ledger.open_count + pending
        within_positions = open_count < params.max_open_positions
        collected.append(
            Reason(
                reasons.RISK_MAX_POSITIONS,
                within_positions,
                {"open": open_count, "limit": params.max_open_positions, "pending": pending},
            )
        )

        collected.extend(self._check_cooldown(ts))
        return collected

    def _check_cooldown(self, ts: int) -> list[Reason]:
        """Sperrfrist nach einem Trade (Spec Paragraph 24).

        `ts` ist MARKTZEIT, nicht Wanduhr. Mit der Wanduhr zu rechnen waere der
        naheliegende Fehler: im Backtest vergehen zwischen zwei Bars
        Mikrosekunden, die Sperre griffe dort nie und im Echtbetrieb immer -
        und damit waeren die beiden nicht mehr vergleichbar (Invariante 3).

        `ts == 0` heisst "Zeit unbekannt" und laesst die Pruefung aus. Das ist
        hier zulaessig, weil die Sperre eine Zusatzbedingung ist und keine
        Sicherheitsgrenze: sie kann einen erlaubten Trade verhindern, aber ihr
        Ausfall erlaubt keinen verbotenen.
        """
        params = self.config.risk
        if ts <= 0:
            return []

        collected: list[Reason] = []
        for minuten, letzter, code in (
            (params.cooldown_minutes_after_loss, self.ledger.last_loss_exit_ts,
             reasons.RISK_COOLDOWN_AFTER_LOSS),
            (params.cooldown_minutes_after_trade, self.ledger.last_exit_ts,
             reasons.RISK_COOLDOWN_AFTER_TRADE),
        ):
            if minuten <= 0 or letzter <= 0:
                continue
            vergangen = (ts - letzter) / 60e9
            if vergangen < minuten:
                collected.append(
                    Reason(
                        code,
                        False,
                        {
                            "vergangen_minuten": round(vergangen, 1),
                            "sperrfrist_minuten": minuten,
                        },
                    )
                )
        return collected

    # ----------------------------------------------------------------- Gesamturteil
    def evaluate(
        self,
        stop_distance_points: float,
        session: str,
        atr: float,
        trading_day: int,
        pending: int = 0,
        ts: int = 0,
    ) -> RiskAssessment:
        collected: list[Reason] = []

        collected.extend(self.check_limits(trading_day, pending, ts))
        if any(not r.ok for r in collected):
            return RiskAssessment(False, None, tuple(collected))

        # Nachrichten vor dem Handelsfenster: beide sind Marktfilter, aber
        # "CPI in drei Minuten" ist die praezisere Auskunft als "falsche
        # Session" - und im Protokoll steht der erste Grund vorn.
        collected.extend(self.check_news(ts))
        if any(not r.ok for r in collected):
            return RiskAssessment(False, None, tuple(collected))

        collected.extend(self.check_trading_window(session, atr))
        if any(not r.ok for r in collected):
            return RiskAssessment(False, None, tuple(collected))

        position = calculate_position_size(stop_distance_points, self.instrument, self.config.risk)
        if not position.ok:
            collected.append(
                Reason(
                    reasons.RISK_SIZE_ZERO,
                    False,
                    {
                        "risk_budget": round(position.risk_budget, 2),
                        "risk_per_contract": round(position.risk_per_contract, 2),
                        "stop_ticks": round(self.instrument.to_ticks(stop_distance_points), 1),
                    },
                )
            )
            return RiskAssessment(False, position, tuple(collected))

        collected.append(
            Reason(
                reasons.RISK_SIZE_OK,
                True,
                {
                    "quantity": position.quantity,
                    "risk_amount": round(position.risk_amount, 2),
                    "risk_budget": round(position.risk_budget, 2),
                },
            )
        )
        if position.capped:
            collected.append(
                Reason(
                    reasons.RISK_SIZE_CAPPED,
                    True,
                    {"quantity": position.quantity, "cap": self.config.risk.max_position_size},
                )
            )

        return RiskAssessment(True, position, tuple(collected))


def session_is_tradeable(session: str) -> bool:
    return session != SessionName.CLOSED.value
