/**
 * Der laufende Betrieb: beobachten und anhalten (Phase 7, Spec 22/24).
 *
 * Zwei Entscheidungen zur Darstellung:
 *
 * 1. **Warnungen stehen ueber den Zahlen.** Ein angehaltener Betrieb mit
 *    huebschem Kontostand sieht sonst aus wie ein laufender. Dieselbe
 *    Reihenfolge wie im Backtest-Bericht.
 *
 * 2. **Der Kill Switch ist immer sichtbar und braucht keine Rueckfrage.**
 *    Ein Not-Aus mit Bestaetigungsdialog ist keiner. Er kostet auch nichts:
 *    er verhindert nur NEUE Positionen, offene laufen weiter zu ihrem Stop -
 *    ein versehentlicher Klick richtet also keinen Schaden an. Das
 *    endgueltige Beenden liegt daneben und ist bewusst unauffaelliger.
 */

import type { SessionStatus, SimulatedTrade } from '../api/types';
import { de } from '../i18n/de';

interface Props {
  status: SessionStatus | null;
  trades: SimulatedTrade[];
  busy: boolean;
  symbol: string;
  onStart: () => void;
  onHalt: () => void;
  onResume: () => void;
  onStop: () => void;
}

const money = (value: number) =>
  value.toLocaleString('de-DE', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

function stateLabel(status: SessionStatus): string {
  if (!status.active) return de.session.stateIdle;
  if (status.halted_reason) return de.session.stateHalted;
  if (!status.connected) return de.session.stateWaiting;
  return de.session.stateRunning;
}

function stateClass(status: SessionStatus): string {
  if (!status.active) return 'idle';
  if (status.halted_reason) return 'halted';
  if (!status.connected) return 'waiting';
  return 'running';
}

export function SessionPanel({
  status,
  trades,
  busy,
  symbol,
  onStart,
  onHalt,
  onResume,
  onStop,
}: Props) {
  if (!status) {
    return (
      <section className="panel panel--session">
        <h2 className="panel__title">{de.session.title}</h2>
        <p className="muted">{de.common.loading}</p>
      </section>
    );
  }

  const pnl = status.realized_pnl;

  return (
    <section className="panel panel--session">
      <h2 className="panel__title">
        {de.session.title}
        <span className={`session-state session-state--${stateClass(status)}`}>
          {stateLabel(status)}
        </span>
      </h2>

      {status.warnings.length > 0 && (
        <ul className="warnings">
          {status.warnings.map((warning, index) => (
            <li key={index}>{warning}</li>
          ))}
        </ul>
      )}

      {status.error && <p className="notice notice--warn">{status.error}</p>}

      {status.active ? (
        <>
          <div className="kv">
            <span>{de.session.feed}</span>
            <code>{status.feed}</code>
          </div>
          <div className="kv">
            <span>{de.session.symbols}</span>
            <code>{status.symbols.join(', ')}</code>
          </div>
          <div className="kv">
            <span>{de.session.bars}</span>
            <span>{status.bars_seen.toLocaleString('de-DE')}</span>
          </div>
          <div className="kv">
            <span>{de.session.signals}</span>
            <span>{status.signals}</span>
          </div>
          <div className="kv">
            <span>{de.session.openPositions}</span>
            <span>{status.open_positions}</span>
          </div>
          <div className="kv">
            <span>{de.session.closedTrades}</span>
            <span>{status.trades_closed}</span>
          </div>
          <div className="kv">
            <span>{de.session.equity}</span>
            <strong className={pnl >= 0 ? 'pos' : 'neg'}>
              {money(status.equity)} USD
            </strong>
          </div>
          <div className="kv">
            <span>{de.session.dayPnl}</span>
            <span className={status.day_pnl >= 0 ? 'pos' : 'neg'}>
              {status.day_pnl >= 0 ? '+' : ''}
              {money(status.day_pnl)} USD
            </span>
          </div>

          <div className="session-actions">
            {status.halted_reason ? (
              <button type="button" className="btn" disabled={busy} onClick={onResume}>
                {de.session.resume}
              </button>
            ) : (
              <button
                type="button"
                className="btn btn--danger"
                disabled={busy}
                onClick={onHalt}
                title={de.session.haltHint}
              >
                {de.session.halt}
              </button>
            )}
            <button type="button" className="btn btn--quiet" disabled={busy} onClick={onStop}>
              {de.session.stop}
            </button>
          </div>
          <p className="muted session-hint">{de.session.haltHint}</p>
        </>
      ) : (
        <>
          <p className="muted">{de.session.idleHint}</p>
          <button
            type="button"
            className="btn"
            disabled={busy || !symbol}
            onClick={onStart}
          >
            {de.session.start} {symbol && <code>{symbol}</code>}
          </button>
          {status.stopped_by && (
            <p className="muted">
              {de.session.stoppedBy}: <code>{status.stopped_by}</code>
            </p>
          )}
        </>
      )}

      {trades.length > 0 && (
        <>
          <h3 className="panel__subtitle">{de.session.trades}</h3>
          <div className="session-trades">
            {trades
              .slice()
              .reverse()
              .map((trade) => (
                <div
                  key={`${trade.setup_id}-${trade.exit_ts}`}
                  className={`session-trades__row ${trade.pnl >= 0 ? 'pos' : 'neg'}`}
                >
                  <span>{trade.strategy}</span>
                  <span>{trade.side}</span>
                  <span>{trade.r_multiple.toFixed(2)} R</span>
                  <span>{money(trade.pnl)}</span>
                  <span className="muted">{trade.exit_reason}</span>
                </div>
              ))}
          </div>
        </>
      )}
    </section>
  );
}
