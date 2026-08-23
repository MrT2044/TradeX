"""Die Sicherheitskette vor jeder Order (Spec Paragraph 24).

Fail closed
-----------
Jede Stufe muss ausdruecklich zustimmen. Es gibt keinen Zweig, in dem eine
unbeantwortete Frage als "vermutlich in Ordnung" durchgeht - Unsicherheit ist
hier ein Nein. Der teuerste denkbare Fehler waere eine Live-Order aus einem
Programm, das sich fuer ein Papertrading-Programm haelt.

Reine Funktionen ohne I/O
-------------------------
Dieses Modul fragt nichts ab und verbindet sich mit nichts. Es bekommt den
Zustand hereingereicht und faellt ein Urteil. Deshalb laesst sich die ganze
Kette in Tests durchspielen, ohne dass ein IB Gateway laeuft - und das ist die
einzige Art, wie sie ueberhaupt geprueft werden kann.

Warum der Port kein Beweis ist
------------------------------
`port == 4002` sagt nur, wohin verbunden wurde. Ein Gateway laesst sich auf
jedem Port betreiben, und die TWS-API kennt kein Feld "dies ist ein
Paper-Konto". Der belastbarste verfuegbare Nachweis ist die tatsaechliche
Kontonummer gegen eine hinterlegte Liste; ersatzweise das Praefix `DU`/`DF`,
das IBKR seit Jahren fuer Paper-Konten verwendet - eine Konvention, keine
zugesicherte Eigenschaft. Genau deshalb steht beides hier und nicht eines
allein.
"""

from __future__ import annotations

from dataclasses import dataclass

from tradex.analysis import reasons as R
from tradex.broker.env import EnvOverrides
from tradex.broker.types import AccountInfo
from tradex.config import BrokerConfig, ExecutionConfig
from tradex.domain.enums import TradingMode
from tradex.persistence.models import Reason

#: Handelsmodi, in denen ueberhaupt Orders entstehen duerfen. Live steht
#: bewusst NICHT dabei: Phase 8 ist Paper, mehr nicht.
PAPER_MODES: frozenset[TradingMode] = frozenset(
    {TradingMode.PAPER_MANUAL, TradingMode.PAPER_AUTO}
)
LIVE_MODES: frozenset[TradingMode] = frozenset(
    {TradingMode.LIVE_MANUAL, TradingMode.LIVE_AUTO}
)


@dataclass(frozen=True, slots=True)
class GateResult:
    """Urteil der Kette. `reasons` enthaelt auch die bestandenen Stufen."""

    approved: bool
    reasons: tuple[Reason, ...] = ()

    @property
    def blocking_reason(self) -> Reason | None:
        return next((reason for reason in self.reasons if not reason.ok), None)

    @property
    def blocking_code(self) -> str:
        blocking = self.blocking_reason
        return blocking.code if blocking is not None else ""


def check_configuration(
    execution: ExecutionConfig,
    broker: BrokerConfig,
    env: EnvOverrides,
) -> GateResult:
    """Stufen 1-4: darf dieses Programm ueberhaupt Orders senden?

    Wird vor dem Verbindungsaufbau gerufen. Faellt sie durch, wird gar nicht
    erst verbunden - eine Sitzung, die sich anmeldet und dann feststellt, dass
    sie nicht handeln darf, hat bereits einen Zustand beim Broker erzeugt.
    """
    reasons: list[Reason] = []

    if execution.mode in LIVE_MODES or execution.live_trading_enabled:
        # Beides zusammen waere die Live-Freigabe. Einzeln ist es ein
        # halbfertiger Zustand - und auch die vollstaendige Freigabe fuehrt
        # hier nicht weiter: Live ist nicht Teil dieser Ausbaustufe.
        return GateResult(
            approved=False,
            reasons=(
                Reason(
                    R.BROKER_LIVE_BLOCKED,
                    False,
                    {
                        "mode": execution.mode.value,
                        "live_trading_enabled": execution.live_trading_enabled,
                    },
                ),
            ),
        )

    if execution.mode not in PAPER_MODES:
        return GateResult(
            approved=False,
            reasons=(Reason(R.BROKER_MODE_NOT_PAPER, False, {"mode": execution.mode.value}),),
        )
    reasons.append(Reason(R.BROKER_MODE_NOT_PAPER, True, {"mode": execution.mode.value}))

    if not broker.enabled:
        reasons.append(Reason(R.BROKER_DISABLED, False, {}))
        return GateResult(approved=False, reasons=tuple(reasons))
    reasons.append(Reason(R.BROKER_DISABLED, True, {"provider": broker.provider}))

    if env.blocks_trading:
        reasons.append(Reason(R.BROKER_TRADING_DISABLED, False, {"quelle": env.source}))
        return GateResult(approved=False, reasons=tuple(reasons))
    reasons.append(Reason(R.BROKER_TRADING_DISABLED, True, {}))

    return GateResult(approved=True, reasons=tuple(reasons))


def check_port(broker: BrokerConfig, port: int) -> Reason:
    """Stufe 5: verbunden wird ausschliesslich auf den Paper-Port."""
    ok = port == broker.ibkr.paper_port
    return Reason(
        R.BROKER_PORT_NOT_PAPER,
        ok,
        {"port": port, "erwartet": broker.ibkr.paper_port},
    )


def confirm_paper_account(
    broker: BrokerConfig,
    accounts: tuple[str, ...],
    account_type: str = "",
) -> AccountInfo:
    """Stufe 7: ist das verbundene Konto nachweislich ein Paper-Konto?

    Liefert immer ein `AccountInfo`; `is_paper` sagt, ob gehandelt werden darf,
    und `paper_evidence` nennt den Grund. Ein Flag ohne Begruendung waere im
    Nachhinein nicht ueberpruefbar.

    Mehrere Konten fuehren zur Ablehnung. Es waere technisch loesbar, sich eins
    auszusuchen - aber "welches Konto hat der Bot eigentlich gehandelt?" ist
    keine Frage, die man aus Bequemlichkeit offen laesst.
    """
    if not accounts:
        return AccountInfo(
            account="",
            account_type=account_type,
            is_paper=False,
            paper_evidence="kein Konto gemeldet",
        )
    if len(accounts) > 1:
        return AccountInfo(
            account=",".join(accounts),
            account_type=account_type,
            is_paper=False,
            paper_evidence=f"{len(accounts)} Konten gemeldet - nicht eindeutig",
        )

    account = accounts[0].strip()
    allowed = tuple(entry.strip().upper() for entry in broker.ibkr.allowed_accounts if entry.strip())
    if allowed:
        # Die Allowlist ist der einzige harte Nachweis. Ist sie gesetzt,
        # entscheidet ausschliesslich sie - ein Praefix duerfte sie nicht
        # aushebeln, sonst waere sie wirkungslos.
        if account.upper() in allowed:
            return AccountInfo(
                account=account,
                account_type=account_type,
                is_paper=True,
                paper_evidence="allowlist",
            )
        return AccountInfo(
            account=account,
            account_type=account_type,
            is_paper=False,
            paper_evidence="nicht in broker.ibkr.allowed_accounts",
        )

    prefixes = tuple(p.strip().upper() for p in broker.ibkr.paper_account_prefixes if p.strip())
    if prefixes and account.upper().startswith(prefixes):
        return AccountInfo(
            account=account,
            account_type=account_type,
            is_paper=True,
            paper_evidence=f"praefix ({'/'.join(prefixes)}) - Konvention, keine API-Zusicherung",
        )
    return AccountInfo(
        account=account,
        account_type=account_type,
        is_paper=False,
        paper_evidence=f"Praefix passt zu keinem von {'/'.join(prefixes) or '(keine)'}",
    )


def check_account(account: AccountInfo | None) -> Reason:
    """Stufe 7 als Reason - fuer die Kette vor jeder einzelnen Order."""
    if account is None:
        return Reason(R.BROKER_ACCOUNT_UNCONFIRMED, False, {"account": None})
    return Reason(
        R.BROKER_ACCOUNT_UNCONFIRMED,
        account.is_paper,
        {"account": account.account, "nachweis": account.paper_evidence},
    )
