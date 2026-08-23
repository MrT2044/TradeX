/**
 * Ueberwachungsansicht fuers Handy.
 *
 * Bewusst NICHT das Desktop-Layout auf schmalem Schirm. Zwei Gruende:
 *
 *   1. Das Chart mit seinen Overlays ist auf einem Telefondisplay nicht
 *      lesbar und kostet dabei am meisten Rechenzeit und Datenvolumen.
 *   2. Diese Ansicht ist zum HINSEHEN da, nicht zum Bedienen. Wer unterwegs
 *      auf ein Telefon schaut, will wissen, ob alles laeuft - nicht einen
 *      Backtest starten.
 *
 * Deshalb stehen hier keine Steuerbefehle: kein Start, kein Resume, keine
 * Risikoeinstellung. Der Not-Aus ist die einzige Ausnahme, die ueberhaupt
 * erwaegenswert waere - und auch der wird erst freigeschaltet, wenn der
 * Fernzugriff steht und eine Anmeldung dahinterliegt.
 *
 * Die wichtigste Zeile ist die oberste: laeuft das System, und entstehen
 * daraus echte Orders? Alles darunter erklaert nur.
 */

import type { SessionStatus, SimulatedTrade } from '../api/types';
import type { StreamMode } from '../api/useSessionStream';
import { de } from '../i18n/de';

interface Props {
  status: SessionStatus | null;
  trades: SimulatedTrade[];
  mode: StreamMode;
  ageSeconds: number;
}

function money(value: number): string {
  return value.toLocaleString('de-DE', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function signed(value: number): string {
  return `${value >= 0 ? '+' : ''}${money(value)}`;
}

function tone(value: number): string {
  if (value > 0) return 'mobile__value mobile__value--ok';
  if (value < 0) return 'mobile__value mobile__value--error';
  return 'mobile__value';
}

/** Ampel fuer eine Zustandszeile. */
function Lamp({ ok, warn }: { ok: boolean; warn?: boolean }) {
  const kind = ok ? 'ok' : warn ? 'warn' : 'error';
  return <span className={`mobile__lamp mobile__lamp--${kind}`} aria-hidden="true" />;
}

function Row({
  label,
  value,
  ok,
  warn,
}: {
  label: string;
  value: string;
  ok: boolean;
  warn?: boolean;
}) {
  return (
    <div className="mobile__row">
      <span className="mobile__row-label">
        <Lamp ok={ok} warn={warn} />
        {label}
      </span>
      <span className="mobile__row-value">{value}</span>
    </div>
  );
}

export function MobileDashboard({ status, trades, mode, ageSeconds }: Props) {
  if (!status) {
    return (
      <div className="mobile">
        <div className="mobile__card mobile__card--muted">{de.mobile.connecting}</div>
      </div>
    );
  }

  const broker = status.broker;
  // Der Stand ist alt, wenn eine Weile nichts mehr kam. Das zu verschweigen
  // waere der gefaehrlichste Fehler dieser Ansicht: veraltete Zahlen, die wie
  // aktuelle aussehen.
  const stale = mode !== 'stream' || ageSeconds > 20;

  return (
    <div className="mobile">
      {stale && (
        <div className="mobile__card mobile__card--stale">
          {mode === 'offline'
            ? de.mobile.offline
            : de.mobile.stale.replace('{s}', String(ageSeconds))}
        </div>
      )}

      {/* --- Der eine Blick, auf den es ankommt --------------------------- */}
      <div className="mobile__card">
        <div className="mobile__headline">
          <Lamp ok={status.accepts_entries} warn={status.active} />
          <span>
            {status.active
              ? status.accepts_entries
                ? de.mobile.running
                : de.mobile.halted
              : de.mobile.idle}
          </span>
        </div>
        {status.halted_reason && (
          <div className="mobile__reason">
            {de.mobile.haltReasons[status.halted_reason] ?? status.halted_reason}
          </div>
        )}
        <div className="mobile__reason">
          {broker.ready ? de.mobile.ordersLive : de.mobile.ordersSimulated}
        </div>
      </div>

      {/* --- Konto --------------------------------------------------------- */}
      <div className="mobile__card">
        <div className="mobile__grid">
          <div className="mobile__cell">
            <span className="mobile__label">{de.mobile.equity}</span>
            <span className="mobile__value">{money(status.equity)}</span>
          </div>
          <div className="mobile__cell">
            <span className="mobile__label">{de.mobile.dayPnl}</span>
            <span className={tone(status.day_pnl)}>{signed(status.day_pnl)}</span>
          </div>
          <div className="mobile__cell">
            <span className="mobile__label">{de.mobile.totalPnl}</span>
            <span className={tone(status.realized_pnl)}>{signed(status.realized_pnl)}</span>
          </div>
          <div className="mobile__cell">
            <span className="mobile__label">{de.mobile.openPositions}</span>
            <span className="mobile__value">{status.open_positions}</span>
          </div>
        </div>
      </div>

      {/* --- Zustaende ----------------------------------------------------- */}
      <div className="mobile__card">
        <Row
          label={de.mobile.feed}
          value={status.connected ? status.feed : de.mobile.disconnected}
          ok={status.connected}
        />
        <Row
          label={de.mobile.broker}
          value={
            !broker.enabled
              ? de.mobile.brokerOff
              : broker.connected
                ? broker.account || broker.provider
                : de.mobile.disconnected
          }
          ok={broker.ready}
          warn={!broker.enabled}
        />
        <Row
          label={de.mobile.openOrders}
          value={String(broker.open_orders)}
          ok={true}
        />
        <Row
          label={de.mobile.signals}
          value={`${status.signals} / ${status.trades_closed} ${de.mobile.tradesShort}`}
          ok={true}
        />
        <Row
          label={de.mobile.bars}
          value={status.bars_seen.toLocaleString('de-DE')}
          ok={status.bars_seen > 0}
          warn={status.active}
        />
      </div>

      {/* --- Warnungen ----------------------------------------------------- */}
      {status.warnings.length > 0 && (
        <div className="mobile__card mobile__card--warn">
          {status.warnings.map((warning) => (
            <div key={warning} className="mobile__warning">
              {warning}
            </div>
          ))}
        </div>
      )}

      {/* --- Letzte Trades -------------------------------------------------- */}
      <div className="mobile__card">
        <div className="mobile__label">{de.mobile.lastTrades}</div>
        {trades.length === 0 ? (
          <div className="mobile__reason">{de.mobile.noTrades}</div>
        ) : (
          trades
            .slice(-8)
            .reverse()
            .map((trade) => (
              <div key={`${trade.trade_id}-${trade.exit_ts}`} className="mobile__trade">
                <span className="mobile__trade-symbol">{trade.symbol}</span>
                <span className="mobile__trade-side">{trade.side}</span>
                <span className={tone(trade.r_multiple)}>{trade.r_multiple.toFixed(2)} R</span>
                <span className={tone(trade.pnl)}>{signed(trade.pnl)}</span>
              </div>
            ))
        )}
      </div>
    </div>
  );
}
