/**
 * Strategie-Panel: Setups im Aufbau, das letzte Signal und - am wichtigsten -
 * warum gerade NICHT gehandelt wird.
 *
 * Beim Zusehen ist die haeufigste Frage nicht "warum dieser Trade?", sondern
 * "warum passiert nichts?". Ohne die Aufstellung der Ablehnungsgruende muesste
 * man sich die Antwort aus Hunderten Einzelentscheidungen zusammensuchen.
 */

import type { StrategyState } from '../api/types';
import { de, translateReason, translateReasonLabel, STOP_ANCHOR } from '../i18n/de';

interface Props {
  strategy: StrategyState | null;
}

const CHAIN = ['sweep', 'displacement', 'fvg', 'retracement', 'mss'] as const;

export function StrategyPanel({ strategy }: Props) {
  if (!strategy) {
    return (
      <section className="panel">
        <h2 className="panel__title">{de.strategy.title}</h2>
        <p className="muted">{de.chart.empty}</p>
      </section>
    );
  }

  const signal = strategy.last_signal;
  const rejections = Object.entries(strategy.rejection_counts);

  return (
    <section className="panel">
      <h2 className="panel__title">{de.strategy.title}</h2>

      <div className="strategy__meta">
        <span>
          {de.strategy.setupTf}: <code>{strategy.setup_timeframe}</code>
        </span>
        <span>
          {de.strategy.confirmTf}: <code>{strategy.confirmation_timeframe}</code>
        </span>
        <span>
          {de.strategy.stopAnchor}:{' '}
          <code>{STOP_ANCHOR[strategy.stop_anchor] ?? strategy.stop_anchor}</code>
        </span>
        <span>
          {de.strategy.minRr}: <code>{strategy.min_rr.toFixed(1)}</code>
        </span>
      </div>

      <div className="strategy__counters">
        <Counter label={de.strategy.decisions} value={strategy.decisions_total} />
        <Counter label={de.strategy.trades} value={strategy.trades_total} tone="ok" />
        <Counter label={de.strategy.noTrades} value={strategy.no_trades_total} tone="muted" />
      </div>

      {/* ---------------------------------------------------- Letztes Signal */}
      <h3 className="panel__subtitle">{de.strategy.lastSignal}</h3>
      {signal ? (
        <div className={`signal signal--${signal.side.toLowerCase()}`}>
          <div className="signal__head">
            <span className="signal__side">{signal.side}</span>
            <span className="signal__rr">
              {de.strategy.rr} {signal.rr.toFixed(2)}
            </span>
          </div>
          <dl className="signal__grid">
            <div>
              <dt>{de.strategy.entry}</dt>
              <dd>{signal.entry.toLocaleString('de-DE')}</dd>
            </div>
            <div>
              <dt>{de.strategy.stop}</dt>
              <dd className="signal__stop">
                {signal.stop.toLocaleString('de-DE')}
                <small>{signal.stop_ticks.toFixed(0)} T</small>
              </dd>
            </div>
            <div>
              <dt>{de.strategy.target}</dt>
              <dd className="signal__target">{signal.target.toLocaleString('de-DE')}</dd>
            </div>
            <div>
              <dt>{de.strategy.quantity}</dt>
              <dd>{signal.quantity}</dd>
            </div>
            <div>
              <dt>{de.strategy.risk}</dt>
              <dd>{signal.risk_amount.toFixed(2)} $</dd>
            </div>
            <div>
              <dt>{de.strategy.reward}</dt>
              <dd>{signal.reward_amount.toFixed(2)} $</dd>
            </div>
          </dl>
          <p className="signal__note">{de.strategy.notExecuted}</p>
        </div>
      ) : (
        <p className="muted">{de.strategy.noSignal}</p>
      )}

      {/* -------------------------------------------------- Setups im Aufbau */}
      <h3 className="panel__subtitle">{de.strategy.activeSetups}</h3>
      {strategy.active_setups.length === 0 ? (
        <p className="muted">{de.strategy.noSetups}</p>
      ) : (
        <ul className="setups">
          {strategy.active_setups.map((setup) => (
            <li key={setup.id} className={`setups__item setups__item--${setup.direction}`}>
              <div className="setups__head">
                <span className="setups__id">#{setup.id}</span>
                <span className="setups__dir">
                  {setup.direction === 'bullish' ? 'LONG' : 'SHORT'}
                </span>
                <span className="setups__stage">
                  {de.strategy.stages[setup.stage] ?? setup.stage}
                </span>
              </div>
              {/* Die Kette als Fortschrittsleiste: auf einen Blick sichtbar,
                  wie weit das Setup ist und woran es gerade haengt. */}
              <div className="chain">
                {CHAIN.map((step) => (
                  <span
                    key={step}
                    className={setup.checklist[step] ? 'chain__step chain__step--ok' : 'chain__step'}
                    title={de.strategy.conditions[step] ?? step}
                  >
                    {de.strategy.conditions[step]?.[0] ?? step[0]}
                  </span>
                ))}
              </div>
              <div className="setups__detail">
                Sweep {setup.sweep_price.toLocaleString('de-DE')} · ungueltig ab{' '}
                {setup.invalidation_price.toLocaleString('de-DE')}
              </div>
            </li>
          ))}
        </ul>
      )}

      {/* ------------------------------------------------ Ablehnungsgruende */}
      {rejections.length > 0 && (
        <>
          <h3 className="panel__subtitle">{de.strategy.whyNoTrade}</h3>
          <ul className="rejections">
            {rejections.map(([code, count]) => (
              <li key={code}>
                <span className="rejections__count">{count}</span>
                <span className="rejections__label">{translateReasonLabel(code)}</span>
              </li>
            ))}
          </ul>
        </>
      )}

      {/* ----------------------------------------- Letzte Einzelentscheidung */}
      {strategy.recent_decisions.length > 0 && (
        <>
          <h3 className="panel__subtitle">{de.analysis.explain}</h3>
          <ul className="reasons">
            {strategy.recent_decisions[strategy.recent_decisions.length - 1].reasons.map(
              (reason, index) => (
                <li key={`${reason.code}-${index}`} className={reason.ok ? 'reasons__ok' : ''}>
                  <span className="reasons__mark">{reason.ok ? '✓' : '✗'}</span>
                  {translateReason(reason.code, reason.params)}
                </li>
              ),
            )}
          </ul>
        </>
      )}
    </section>
  );
}

function Counter({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone?: 'ok' | 'muted';
}) {
  return (
    <div className={`counter${tone ? ` counter--${tone}` : ''}`}>
      <span className="counter__value">{value.toLocaleString('de-DE')}</span>
      <span className="counter__label">{label}</span>
    </div>
  );
}
