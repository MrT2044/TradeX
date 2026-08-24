/** Kopfzeile: Instrument, Marktzustand, Modus, Datenquelle (Spec 22). */

import type { ContextSnapshot, Coverage, Health, Instrument } from '../api/types';
import { de } from '../i18n/de';

interface Props {
  health: Health | null;
  instrument: Instrument | null;
  snapshot: ContextSnapshot | null;
  coverage: Coverage[];
  symbols: string[];
  /** Welche Symbole gespeicherte Historie haben. */
  withData: Set<string>;
  /** Name des Feeds einer laufenden Sitzung, sonst leer. */
  liveFeed: string;
  selected: string;
  onSelect: (symbol: string) => void;
  busy: boolean;
}

export function StatusBar({
  health,
  instrument,
  snapshot,
  coverage,
  symbols,
  withData,
  liveFeed,
  selected,
  onSelect,
  busy,
}: Props) {
  const session = snapshot?.session ?? 'closed';
  const marketOpen = session !== 'closed';
  const barCount = coverage
    .filter((c) => c.symbol === selected)
    .reduce((total, c) => Math.max(total, c.bar_count), 0);

  // Ein Demo-Symbol muss auf den ersten Blick als solches erkennbar sein.
  // Synthetische Daten sehen im Chart genauso aus wie echte - der einzige
  // Schutz davor, Schluesse daraus zu ziehen, ist diese Kennzeichnung.
  const isDemo = selected.endsWith('_DEMO');

  return (
    <header className={`statusbar${isDemo ? ' statusbar--demo' : ''}`}>
      <div className="statusbar__brand">
        <span className="statusbar__logo">TradeX</span>
        <span className="statusbar__subtitle">{de.app.subtitle}</span>
      </div>

      <div className="statusbar__items">
        <label className="field">
          <span className="field__label">{de.status.symbol}</span>
          <select
            className="field__input"
            value={selected}
            disabled={busy || symbols.length === 0}
            onChange={(event) => onSelect(event.target.value)}
          >
            {/* Leerer Eintrag als Ausgangszustand: der Start waehlt nichts
                mehr von selbst, also muss die Auswahl auch zeigen koennen,
                dass noch nichts gewaehlt ist. */}
            {!selected && <option value="">{de.status.chooseSymbol}</option>}
            {symbols.map((symbol) => (
              <option key={symbol} value={symbol}>
                {/* Ohne gespeicherte Historie sind das die Instrumente, deren
                    Bars live hereinkommen (MNQ, NQ). Sie gehoeren in die
                    Liste, aber man muss den Unterschied sehen, bevor man
                    waehlt - nicht erst am leeren Chart danach. */}
                {symbol}
                {withData.has(symbol) ? '' : ` ${de.status.liveOnly}`}
              </option>
            ))}
          </select>
        </label>

        <Item label={de.status.market}>
          <span className={marketOpen ? 'pill pill--ok' : 'pill pill--off'}>
            {marketOpen ? de.status.open : de.status.closed}
          </span>
        </Item>

        <Item label={de.status.session}>
          {de.status.sessions[session] ?? session}
        </Item>

        <Item label={de.status.mode}>
          <span className="pill pill--info">
            {health ? (de.status.modes[health.mode] ?? health.mode) : '-'}
          </span>
        </Item>

        {/* Laeuft eine Sitzung, ist DEREN Feed die Datenquelle - nicht die
            Liste der registrierten Provider. Die aendert sich nie und stand
            deshalb auch waehrend eines NT8-Betriebs auf "replay": eine Anzeige,
            die im Echtbetrieb die Wiedergabe meldet, ist schlimmer als keine. */}
        <Item label={de.status.dataFeed}>
          {liveFeed ? (
            <span className="pill pill--ok">{liveFeed}</span>
          ) : (
            health?.providers.map((p) => p.name).join(', ') || '-'
          )}
        </Item>

        <Item label={de.status.bars}>{barCount ? barCount.toLocaleString('de-DE') : '-'}</Item>

        {instrument && (
          <Item label="Tick">
            {instrument.tick_size} = {instrument.tick_value.toFixed(2)} {instrument.currency}
          </Item>
        )}
      </div>

      {isDemo && (
        <div className="statusbar__demo-banner">
          SYNTHETISCHE DEMODATEN &ndash; keine Marktdaten. Nur zum Pruefen der Oberflaeche und
          der Detektoren geeignet, nicht fuer Aussagen ueber die Strategie.
        </div>
      )}
    </header>
  );
}

function Item({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="statusbar__item">
      <span className="statusbar__item-label">{label}</span>
      <span className="statusbar__item-value">{children}</span>
    </div>
  );
}
