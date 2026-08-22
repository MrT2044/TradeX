"""Mehrere Strategien, ein Konto (Spec §10, §24).

Das Portfolio ist die Stelle, an der aus Vorschlaegen Entscheidungen werden.
Es fuehrt N Strategien und genau EINE Risikopruefung.

Warum das Risiko zentral gehoert
--------------------------------
Risikobudget, Tagesverlustlimit und die Obergrenze fuer offene Positionen
gelten fuer das Konto, nicht fuer eine Strategie. Rechnete jede Strategie ihre
eigene Groesse, waere das tatsaechliche Gesamtrisiko die Summe der
Einzelbudgets - bei drei Strategien also das Dreifache des Erlaubten. Genau so
sprengt man ein Konto, waehrend jede einzelne Komponente "korrekt" arbeitet.

Reihenfolge ist Teil der Regel
------------------------------
Schlagen mehrere Strategien auf derselben Bar etwas vor, ist bei knappen
Grenzen nicht mehr jedes davon moeglich. Wer zuerst geprueft wird, entscheidet
also mit. Sortiert wird deshalb deterministisch - nach Strategiename und
Setup-Nummer, nicht nach Eintreffreihenfolge. Ohne das waere derselbe Lauf
nicht reproduzierbar, sobald sich an der Reihenfolge der Registry etwas
aendert.

Fairness zwischen Strategien
----------------------------
Bewusst KEINE Gewichtung, kein Vorrang, keine Quote. Wer zuerst kommt, mahlt
zuerst - und "zuerst" ist alphabetisch. Eine Bevorzugung waere eine Aussage
darueber, welche Strategie besser ist; genau diese Aussage soll erst der
Backtest treffen.
"""

from __future__ import annotations

from tradex.analysis import reasons as R
from tradex.analysis.context import MarketContext, TimeframeUpdate
from tradex.config import Config
from tradex.domain.instruments import Instrument
from tradex.logging_setup import get_logger
from tradex.persistence.models import Reason
from tradex.risk.engine import RiskEngine
from tradex.risk.ledger import RiskLedger
from tradex.strategy.base import Strategy, TradeProposal
from tradex.strategy.signal import StrategyDecision, TradeSignal

log = get_logger(__name__)


class StrategyPortfolio:
    """Fuehrt mehrere Strategien und faellt die Handelsentscheidungen."""

    def __init__(
        self,
        symbol: str,
        instrument: Instrument,
        config: Config,
        strategies: list[Strategy],
        ledger: RiskLedger | None = None,
    ) -> None:
        if not strategies:
            raise ValueError("Ein Portfolio ohne Strategien kann nichts entscheiden")
        names = [s.name for s in strategies]
        if len(set(names)) != len(names):
            raise ValueError(f"Strategienamen muessen eindeutig sein: {names}")

        self.symbol = symbol.upper()
        self.instrument = instrument
        self.config = config
        self.strategies = strategies
        self.ledger = ledger or RiskLedger()
        self.risk = RiskEngine(config, instrument, self.ledger)

        self.decisions: list[StrategyDecision] = []
        self.signals: list[TradeSignal] = []

    # ------------------------------------------------------------------ Zugriff
    def strategy(self, name: str) -> Strategy:
        for item in self.strategies:
            if item.name == name:
                return item
        known = ", ".join(s.name for s in self.strategies)
        raise KeyError(f"Unbekannte Strategie {name!r}. Bekannt: {known}")

    def reset(self) -> None:
        for item in self.strategies:
            item.reset()
        self.ledger.reset()
        self.decisions = []
        self.signals = []

    # -------------------------------------------------------------------- Lauf
    def on_updates(
        self, updates: list[TimeframeUpdate], context: MarketContext
    ) -> list[StrategyDecision]:
        """Alle Strategien befragen und die Vorschlaege gegen das Risiko pruefen."""
        if not updates:
            return []

        produced: list[StrategyDecision] = []
        proposals: list[TradeProposal] = []

        for item in self.strategies:
            out = item.on_updates(updates, context)
            produced.extend(out.decisions)
            proposals.extend(out.proposals)

        # Deterministische Reihenfolge - siehe Modul-Docstring.
        proposals.sort(key=lambda p: (p.strategy, p.setup_id))

        # `approved` zaehlt, was auf DIESER Bar schon durchgelassen wurde. Ohne
        # den Zaehler saehen alle Vorschlaege dieselbe leere Position und kaemen
        # alle durch (Spec §24, Duplicate Order Protection).
        approved = 0
        for proposal in proposals:
            decision = self._judge(proposal, approved)
            if decision.is_trade:
                approved += 1
            produced.append(decision)

        self.decisions.extend(produced)
        return produced

    # ------------------------------------------------------------------ Urteil
    def _judge(self, proposal: TradeProposal, approved: int) -> StrategyDecision:
        """Einen Vorschlag gegen Grenzen, Handelsfenster und Budget pruefen."""
        collected = list(proposal.reasons)

        assessment = self.risk.evaluate(
            proposal.stop_points,
            proposal.session,
            proposal.atr,
            proposal.trading_day,
            pending=approved,
        )
        collected.extend(assessment.reasons)

        if not assessment.approved or assessment.position is None:
            collected.append(
                Reason(
                    R.DECISION_NO_TRADE,
                    False,
                    {"stage": proposal.stage, "strategy": proposal.strategy},
                )
            )
            return self._decision(proposal, "NO_TRADE", collected, None)

        position = assessment.position
        # Die Nummer kommt aus dem Risikobuch: sie muss ueber ALLES eindeutig
        # sein, was sich das Konto teilt - Strategien wie Instrumente.
        signal = TradeSignal(
            trade_id=self.ledger.next_trade_id(),
            setup_id=proposal.setup_id,
            symbol=proposal.symbol,
            direction=proposal.direction,
            entry=proposal.entry,
            stop=proposal.stop,
            target=proposal.target,
            stop_ticks=proposal.stop_ticks,
            target_points=proposal.target_points,
            rr=proposal.rr,
            quantity=position.quantity,
            risk_amount=position.risk_amount,
            reward_amount=self.instrument.points_to_currency(
                proposal.target_points, position.quantity
            ),
            entry_ts=proposal.ts,
            entry_index=proposal.index,
            stop_anchor=proposal.stop_anchor,
            target_source=proposal.target_source,
            timeframe=proposal.timeframe,
            strategy=proposal.strategy,
        )
        self.signals.append(signal)

        collected.append(
            Reason(
                R.DECISION_TRADE,
                True,
                {
                    "side": signal.side,
                    "entry": signal.entry,
                    "stop": signal.stop,
                    "target": signal.target,
                    "rr": round(signal.rr, 2),
                    "quantity": signal.quantity,
                    "strategy": proposal.strategy,
                },
            )
        )
        log.info(
            "trade_signal",
            symbol=self.symbol,
            strategy=proposal.strategy,
            setup=signal.setup_id,
            side=signal.side,
            entry=signal.entry,
            stop=signal.stop,
            target=signal.target,
            rr=round(signal.rr, 2),
            quantity=signal.quantity,
        )
        return self._decision(proposal, signal.side, collected, signal)

    def _decision(
        self,
        proposal: TradeProposal,
        verdict: str,
        reasons: list[Reason],
        signal: TradeSignal | None,
    ) -> StrategyDecision:
        return StrategyDecision(
            ts=proposal.ts,
            index=proposal.index,
            symbol=proposal.symbol,
            timeframe=proposal.timeframe,
            setup_id=proposal.setup_id,
            direction=proposal.direction,
            decision=verdict,
            stage=proposal.stage,
            checklist=proposal.checklist,
            reasons=tuple(reasons),
            signal=signal,
            htf_bias=proposal.htf_bias,
            strategy=proposal.strategy,
        )

    # ------------------------------------------------------------------ Abfragen
    def active_candidates(self) -> list[object]:
        collected: list[object] = []
        for item in self.strategies:
            collected.extend(item.active_setups())
        return collected

    def recent_decisions(self, limit: int = 50) -> list[StrategyDecision]:
        return self.decisions[-limit:]

    def stats(self) -> dict[str, int]:
        trades = sum(1 for d in self.decisions if d.is_trade)
        return {
            "decisions": len(self.decisions),
            "trades": trades,
            "no_trades": len(self.decisions) - trades,
            "active_setups": len(self.active_candidates()),
            "strategies": len(self.strategies),
        }

    def stats_per_strategy(self) -> dict[str, dict[str, int]]:
        """Wie viel hat welche Strategie beigetragen?

        Ohne diese Aufschluesselung waere bei mehreren Strategien nicht mehr
        erkennbar, welche davon ueberhaupt etwas liefert - und welche nur
        Ablehnungen produziert.
        """
        result: dict[str, dict[str, int]] = {
            item.name: {"decisions": 0, "trades": 0, "no_trades": 0}
            for item in self.strategies
        }
        for decision in self.decisions:
            entry = result.setdefault(
                decision.strategy, {"decisions": 0, "trades": 0, "no_trades": 0}
            )
            entry["decisions"] += 1
            entry["trades" if decision.is_trade else "no_trades"] += 1
        return result
