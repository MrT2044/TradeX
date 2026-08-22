/**
 * Backtest-Panel (Spec 19).
 *
 * Die Reihenfolge der Abschnitte ist Absicht und beantwortet drei Fragen:
 *
 *   1. Darf man diese Zahlen ueberhaupt lesen?  -> Warnungen, ganz oben
 *   2. Was ist herausgekommen?                  -> Kennzahlen, Kontoverlauf
 *   3. Woher kam es?                            -> Aufschluesselungen, Ablehnungen
 *
 * Punkt 1 steht zuerst, weil er am haeufigsten uebersehen wird: ein
 * Erwartungswert aus zwoelf Trades sieht genauso aus wie einer aus
 * zwoelfhundert. Deshalb erscheinen die Kennzahlen bei zu kleiner Stichprobe
 * gedaempft - sie sind dann noch kein Befund.
 */

import type { BacktestReport, EquityPoint, Metrics } from '../api/types';
import { Info } from '../components/Info';
import { EXIT_REASON, STRATEGY_LABEL, de, translateReasonLabel } from '../i18n/de';

interface Props {
  report: BacktestReport | null;
  busy: boolean;
  onRun: () => void;
}

const SESSION_LABEL = de.status.sessions;

export function BacktestPanel({ report, busy, onRun }: Props) {
  return (
    <section className="panel">
      <h2 className="panel__title">{de.backtest.title}</h2>
      <p className="muted panel__hint">{de.backtest.hint}</p>

      <button type="button" className="button button--primary" onClick={onRun} disabled={busy}>
        {busy ? de.backtest.running : de.backtest.run}
      </button>

      {!report && <p className="muted">{de.backtest.never}</p>}
      {report && <Report report={report} />}
    </section>
  );
}

function Report({ report }: { report: BacktestReport }) {
  const m = report.overall;
  const dimmed = report.is_significant ? '' : ' backtest--dimmed';

  return (
    <>
      {report.warnings.length > 0 && (
        <div className="notice notice--warn backtest__warnings">
          <strong>{de.backtest.readFirst}</strong>
          <ul>
            {report.warnings.map((warning, index) => (
              <li key={index}>{warning}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="kv">
        <span>{de.backtest.period}</span>
        <code>
          {formatDate(report.first_ts)} – {formatDate(report.last_ts)}
        </code>
      </div>
      <div className="kv">
        <span>{de.backtest.signals}</span>
        <code>
          {report.signals} · {report.trades_total} {de.backtest.filled}
          {report.unfilled > 0 ? ` · ${report.unfilled} ${de.backtest.unfilled}` : ''}
          {report.stale > 0 ? ` · ${report.stale} ${de.backtest.stale}` : ''}
        </code>
      </div>

      {m.trades === 0 ? (
        <p className="notice notice--warn">{de.backtest.noTrades}</p>
      ) : (
        <div className={`backtest${dimmed}`}>
          {!report.is_significant && (
            <p className="muted backtest__caveat">{de.backtest.notSignificant}</p>
          )}

          <div className="strategy__counters">
            <Counter label={de.backtest.trades} value={String(m.trades)} />
            <Counter label={de.backtest.winRate} value={`${m.win_rate.toFixed(1)} %`} />
            <Counter
              label={de.backtest.expectancy}
              value={`${signed(m.expectancy_r, 2)} R`}
              tone={m.expectancy_r > 0 ? 'ok' : 'bad'}
              hint={de.backtest.expectancyHint}
            />
          </div>

          <dl className="metrics">
            <Row
              label={de.backtest.profitFactor}
              value={optional(m.profit_factor, 2)}
              hint={de.backtest.profitFactorHint}
            />
            <Row label={de.backtest.payoff} value={optional(m.payoff_ratio, 2)} />
            <Row label={de.backtest.sqn} value={optional(m.sqn, 2)} hint={de.backtest.sqnHint} />
            <Row label={de.backtest.netPnl} value={`${signed(m.net_pnl, 2)} $`} />
            <Row label={de.backtest.commission} value={`-${m.commission.toFixed(2)} $`} />
            <Row label={de.backtest.finalEquity} value={`${m.final_equity.toFixed(2)} $`} />
            <Row label={de.backtest.returnPct} value={`${signed(m.return_pct, 2)} %`} />
            <Row
              label={de.backtest.drawdown}
              value={`${m.max_drawdown_usd.toFixed(2)} $ (${m.max_drawdown_pct.toFixed(2)} %)`}
              hint={de.backtest.drawdownHint}
            />
            <Row label={de.backtest.lossStreak} value={String(m.max_consecutive_losses)} />
            <Row label={de.backtest.holding} value={`${m.avg_bars_held.toFixed(0)} Bars`} />
            <Row
              label={de.backtest.mae}
              value={`${m.avg_mae_r.toFixed(2)} R / ${m.avg_mfe_r.toFixed(2)} R`}
              hint={de.backtest.maeHint}
            />
          </dl>

          <h3 className="panel__subtitle">{de.backtest.equity}</h3>
          <EquityCurve points={report.equity} start={m.start_equity} />

          <h3 className="panel__subtitle">
            {de.backtest.halves}
            <Info text={de.backtest.halvesHint} />
          </h3>
          <Table
            rows={[
              [de.backtest.firstHalf, report.in_sample],
              [de.backtest.secondHalf, report.out_of_sample],
            ]}
          />

          <h3 className="panel__subtitle">
            {de.backtest.byStrategy}
            <Info text={de.backtest.byStrategyHint} />
          </h3>
          <Table
            rows={Object.entries(report.by_strategy).map(([name, value]) => [
              STRATEGY_LABEL[name] ?? name,
              value,
            ])}
          />

          <h3 className="panel__subtitle">{de.backtest.bySession}</h3>
          <Table
            rows={Object.entries(report.by_session).map(([name, value]) => [
              SESSION_LABEL[name] ?? name,
              value,
            ])}
          />

          <h3 className="panel__subtitle">{de.backtest.byDirection}</h3>
          <Table rows={Object.entries(report.by_direction)} />

          <h3 className="panel__subtitle">{de.backtest.byExit}</h3>
          <Table
            rows={Object.entries(report.by_exit).map(([name, value]) => [
              EXIT_REASON[name] ?? name,
              value,
            ])}
          />
        </div>
      )}

      {Object.keys(report.rejections).length > 0 && (
        <>
          <h3 className="panel__subtitle">{de.backtest.whyNoTrade}</h3>
          <ul className="rejections">
            {Object.entries(report.rejections)
              .slice(0, 8)
              .map(([code, count]) => (
                <li key={code}>
                  <span className="rejections__count">{count}</span>
                  <span className="rejections__label">{translateReasonLabel(code)}</span>
                </li>
              ))}
          </ul>
        </>
      )}

      <h3 className="panel__subtitle">
        {de.backtest.assumptions}
        <Info text={de.backtest.assumptionsHint} />
      </h3>
      <dl className="metrics metrics--compact">
        {Object.entries(report.assumptions).map(([key, value]) => (
          <Row key={key} label={key} value={String(value)} />
        ))}
      </dl>
    </>
  );
}

/**
 * Kontoverlauf als SVG-Pfad.
 *
 * Bewusst ohne Chartbibliothek: es geht um die Form des Verlaufs, nicht um
 * ablesbare Einzelwerte. Die Nulllinie ist das Startkapital - ein Verlauf,
 * der darunter bleibt, muss auch so aussehen.
 */
function EquityCurve({ points, start }: { points: EquityPoint[]; start: number }) {
  if (points.length < 2) return <p className="muted">{de.chart.empty}</p>;

  const width = 280;
  const height = 70;
  const values = points.map((p) => p.equity);
  const min = Math.min(start, ...values);
  const max = Math.max(start, ...values);
  const span = max - min || 1;

  const x = (index: number) => (index / (points.length - 1)) * width;
  const y = (value: number) => height - ((value - min) / span) * height;

  const path = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(p.equity).toFixed(1)}`).join(' ');
  const last = values[values.length - 1];

  return (
    <svg className="equity" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={de.backtest.equity}>
      <line className="equity__base" x1={0} y1={y(start)} x2={width} y2={y(start)} />
      <path className={last >= start ? 'equity__line equity__line--up' : 'equity__line'} d={path} />
    </svg>
  );
}

function Table({ rows }: { rows: [string, Metrics][] }) {
  const filled = rows.filter(([, value]) => value.trades > 0);
  if (filled.length === 0) return <p className="muted">–</p>;

  return (
    <table className="metrics-table">
      <thead>
        <tr>
          <th />
          <th>{de.backtest.trades}</th>
          <th>{de.backtest.winRate}</th>
          <th>{de.backtest.rMultiple}</th>
        </tr>
      </thead>
      <tbody>
        {filled.map(([label, value]) => (
          <tr key={label}>
            <td>{label}</td>
            <td>{value.trades}</td>
            <td>{value.win_rate.toFixed(0)} %</td>
            <td className={value.expectancy_r > 0 ? 'positive' : 'negative'}>
              {signed(value.expectancy_r, 2)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Row({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="metrics__row">
      <dt>
        {label}
        {hint && <Info text={hint} />}
      </dt>
      <dd>{value}</dd>
    </div>
  );
}

function Counter({
  label,
  value,
  tone,
  hint,
}: {
  label: string;
  value: string;
  tone?: 'ok' | 'bad';
  hint?: string;
}) {
  return (
    <div className={`counter${tone ? ` counter--${tone}` : ''}`}>
      <span className="counter__value">{value}</span>
      <span className="counter__label">
        {label}
        {hint && <Info text={hint} />}
      </span>
    </div>
  );
}

const signed = (value: number, digits: number): string =>
  `${value >= 0 ? '+' : ''}${value.toFixed(digits)}`;

/** null heisst "aus dieser Stichprobe nicht bestimmbar" - nicht 0. */
const optional = (value: number | null, digits: number): string =>
  value === null ? '–' : value.toFixed(digits);

const formatDate = (ts: number): string =>
  new Date(ts / 1_000_000).toLocaleDateString('de-DE', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  });
