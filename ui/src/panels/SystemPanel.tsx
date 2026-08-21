/** Systemzustand, Datenquellen und Datenqualitaet (Spec 22, 24). */

import type { Health, Integrity, LogEntry } from '../api/types';
import { de } from '../i18n/de';

interface Props {
  health: Health | null;
  integrity: Integrity | null;
  logs: LogEntry[];
}

export function SystemPanel({ health, integrity, logs }: Props) {
  return (
    <section className="panel panel--system">
      <h2 className="panel__title">{de.system.title}</h2>

      {health && (
        <>
          <div className="kv">
            <span>{de.system.strategyVersion}</span>
            <code>{health.strategy_version}</code>
          </div>
          <div className="kv">
            <span>{de.system.configHash}</span>
            <code>{health.config_hash}</code>
          </div>

          <h3 className="panel__subtitle">{de.system.providers}</h3>
          <ul className="providers">
            {health.providers.map((provider) => (
              <li key={provider.name}>
                <div className="providers__head">
                  <span className={`dot dot--${provider.status}`} />
                  <strong>{provider.name}</strong>
                  <span className="muted">{provider.status}</span>
                </div>
                <div className="providers__caps">
                  {[
                    ['Historie', provider.historical_bars],
                    ['Live', provider.live_bars],
                    ['Ticks', provider.live_ticks],
                    ['Bid/Ask', provider.bid_ask],
                    ['Tiefe', provider.market_depth],
                    ['Volumen', provider.volume],
                    ['Orders', provider.order_execution],
                  ].map(([label, enabled]) => (
                    <span
                      key={String(label)}
                      className={enabled ? 'cap cap--on' : 'cap cap--off'}
                    >
                      {String(label)}
                    </span>
                  ))}
                </div>
                {provider.notes && <p className="providers__note">{provider.notes}</p>}
              </li>
            ))}
          </ul>
        </>
      )}

      {integrity && (
        <>
          <h3 className="panel__subtitle">{de.system.integrity}</h3>
          {integrity.is_clean ? (
            <p className="notice notice--ok">{de.system.clean}</p>
          ) : (
            <div className="notice notice--warn">
              <div>
                {de.system.gaps}: {integrity.gaps.length} ({integrity.missing_bars}{' '}
                {de.system.missingBars})
              </div>
              {integrity.invalid_bars.length > 0 && (
                <div>
                  {integrity.invalid_bars.length} {de.system.invalidBars}
                </div>
              )}
            </div>
          )}
        </>
      )}

      {health && health.warnings.length > 0 && (
        <ul className="warnings">
          {health.warnings.map((warning, index) => (
            <li key={index}>{warning}</li>
          ))}
        </ul>
      )}

      <h3 className="panel__subtitle">{de.system.logs}</h3>
      <div className="logs">
        {logs.length === 0 && <p className="muted">{de.system.noLogs}</p>}
        {logs
          .slice()
          .reverse()
          .map((entry, index) => (
            <div key={index} className={`logs__row logs__row--${entry.level}`}>
              <span className="logs__time">{entry.timestamp.slice(11, 19)}</span>
              <span className="logs__event">{entry.event}</span>
              <span className="logs__fields">
                {Object.entries(entry.fields)
                  .map(([key, value]) => `${key}=${value}`)
                  .join(' ')}
              </span>
            </div>
          ))}
      </div>
    </section>
  );
}
