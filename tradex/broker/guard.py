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

Zwei Anbindungen, zwei Nachweise
--------------------------------
Die Stufen 1-4 (`check_configuration`) gelten fuer beide: Modus, Freigabe,
`.env`. Ab Stufe 5 unterscheiden sie sich, weil die Anbindungen verschieden
starke Auskuenfte geben.

**IBKR** (`check_port`, `confirm_paper_account`): `port == 4002` sagt nur,
wohin verbunden wurde - ein Gateway laesst sich auf jedem Port betreiben, und
die TWS-API kennt kein Feld "dies ist ein Paper-Konto". Der belastbarste
verfuegbare Nachweis ist die Kontonummer gegen eine hinterlegte Liste;
ersatzweise das Praefix `DU`/`DF` - eine Konvention, keine zugesicherte
Eigenschaft. Deshalb steht dort beides und nicht eines allein.

**NinjaTrader** (`confirm_simulated_account`): `Account.Provider ==
Provider.Simulator` ist eine Eigenschaft des KONTOS. Der Nachweis ist damit
direkt statt indirekt - das ist der eigentliche Gewinn der Migration. Er wird
im AddOn gefaellt, also auf der Seite, die dieses Programm nicht kontrolliert;
die Pruefung hier ist die zweite Haelfte davon.
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
    """Stufe 5 fuer IBKR: verbunden wird ausschliesslich auf den Paper-Port.

    Fuer NinjaTrader gibt es dazu bewusst KEIN Gegenstueck. Der Bridge-Port
    trennt nicht zwischen Simulation und Echtgeld - er ist nur die Adresse des
    AddOns. Eine Portpruefung dort waere eine Stufe, die aussieht wie ein
    Nachweis und keiner ist; die Kontosperre traegt das allein.
    """
    ok = port == broker.ibkr.paper_port
    return Reason(
        R.BROKER_PORT_NOT_PAPER,
        ok,
        {"port": port, "erwartet": broker.ibkr.paper_port},
    )


def confirm_simulated_account(
    account: str,
    is_simulation: bool,
    provider: str = "",
    allowed_accounts: tuple[str, ...] = (),
) -> AccountInfo:
    """Stufe 7 fuer NinjaTrader - und der Grund, warum die Migration sich lohnt.

    Bei IBKR blieb der Paper-Nachweis strukturell indirekt: Port, `DU`-Praefix
    und Allowlist zusammen, weil die TWS-API kein Feld "dies ist ein
    Paper-Konto" kennt. Ein Praefix ist eine Konvention, keine zugesicherte
    Eigenschaft.

    Hier ist er direkt. `is_simulation` stammt aus `Account.Provider ==
    Provider.Simulator`, einer Eigenschaft des KONTOS, entschieden im AddOn -
    also auf der Seite, die TradeX nicht kontrolliert. Diese Pruefung hier ist
    die zweite Haelfte: eine Sicherheitskette, die nur dort laeuft, wo man
    selbst schreibt, beschreibt die Grenze, statt sie zu pruefen.

    `allowed_accounts` ist optional und wirkt NUR zusaetzlich. Sie kann ein
    Simulationskonto ausschliessen, aber nie eines freischalten, das keines
    ist - sonst waere sie ein Schalter an der Kontosperre vorbei.
    """
    name = account.strip()
    if not name:
        return AccountInfo(
            account="",
            account_type=provider,
            is_paper=False,
            paper_evidence="kein Konto gemeldet",
        )

    if not is_simulation:
        return AccountInfo(
            account=name,
            account_type=provider,
            is_paper=False,
            paper_evidence=f"Account.Provider={provider or '?'} ist nicht Simulator",
        )

    erlaubt = tuple(e.strip().upper() for e in allowed_accounts if e.strip())
    if erlaubt and name.upper() not in erlaubt:
        # Die Liste engt ein, sie erlaubt nicht. Ein Konto, das der Simulator
        # fuehrt, aber nicht auf der Liste steht, wird abgelehnt - etwa das
        # `Backtest`-Konto, das ebenfalls Provider.Simulator ist.
        return AccountInfo(
            account=name,
            account_type=provider,
            is_paper=False,
            paper_evidence="Simulationskonto, aber nicht in broker.nt8.allowed_accounts",
        )

    nachweis = "Account.Provider=Simulator (NinjaTrader)"
    if erlaubt:
        nachweis += " + allowlist"
    return AccountInfo(
        account=name,
        account_type=provider,
        is_paper=True,
        paper_evidence=nachweis,
    )


def check_simulated_account(account: AccountInfo | None) -> Reason:
    """Die NinjaTrader-Stufe als Reason - fuer die Kette vor jeder Order.

    Eigener Code statt `BROKER_ACCOUNT_UNCONFIRMED`: die beiden sagen
    Verschiedenes. "Unbestaetigt" heisst, der Nachweis fehlt; "nicht
    simuliert" heisst, er liegt vor und faellt negativ aus. Im Protokoll ist
    das der Unterschied zwischen "wir wissen es nicht" und "wir wissen, dass
    nicht".
    """
    if account is None:
        return Reason(R.BROKER_ACCOUNT_NOT_SIMULATED, False, {"account": None})
    return Reason(
        R.BROKER_ACCOUNT_NOT_SIMULATED,
        account.is_paper,
        {"account": account.account, "nachweis": account.paper_evidence},
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
