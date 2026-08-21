/**
 * Analyse-Panel: Was hat der Bot gerade erkannt - und warum?
 *
 * Spec 23: Der Nutzer muss nachvollziehen koennen, was passiert. Deshalb wird
 * jede Bedingung mit Haken oder Kreuz gezeigt UND mit dem Grund dahinter.
 *
 * Spec 9: Fehlt eine Pflichtbedingung, gilt KEIN SETUP. Der Bot ergaenzt nichts.
 * Genau dieser Zustand wird hier gross angezeigt - eine unvollstaendige Kette
 * ist die haeufigste und wichtigste Aussage des Systems.
 */

import type { ContextSnapshot, TimeframeSnapshot } from '../api/types';
import { Term } from '../components/Info';
import { de, translateReason } from '../i18n/de';

interface Props {
  snapshot: ContextSnapshot | null;
  entryTimeframe: string;
}

const BIAS_LABEL: Record<string, string> = {
  bullish: 'STEIGEND',
  bearish: 'FALLEND',
  neutral: 'RICHTUNGSLOS',
};

export function AnalysisPanel({ snapshot, entryTimeframe }: Props) {
  if (!snapshot) {
    return (
      <section className="panel">
        <h2 className="panel__title">{de.analysis.title}</h2>
        <p className="muted">{de.chart.empty}</p>
      </section>
    );
  }

  const bias = snapshot.bias;
  const entry: TimeframeSnapshot | undefined = snapshot.timeframes[entryTimeframe];

  // Die Pflichtkette aus Spec 7. In Phase 2 werden die ersten fuenf Glieder
  // ausgewertet; Ruecklauf und Einstieg folgen mit der Strategy Engine.
  const checks = [
    {
      key: 'bias',
      label: de.analysis.htfBias,
      explanation: de.glossary.htfBias,
      ok: bias.bias !== 'neutral',
      value: BIAS_LABEL[bias.bias] ?? bias.bias,
    },
    {
      key: 'liquidity',
      label: de.analysis.liquidity,
      explanation: de.glossary.sweep,
      ok: Boolean(entry?.recent_sweeps.length),
      value: entry?.recent_sweeps.length
        ? `${entry.recent_sweeps.length} Sweep(s)`
        : 'kein Sweep',
    },
    {
      key: 'displacement',
      label: de.analysis.displacement,
      explanation: de.glossary.displacement,
      ok: Boolean(entry?.last_displacement),
      value: entry?.last_displacement
        ? `${entry.last_displacement.range_atr_mult.toFixed(2)}x ATR`
        : de.analysis.none,
    },
    {
      key: 'fvg',
      label: de.analysis.fvg,
      explanation: de.glossary.fvg,
      ok: Boolean(entry?.active_fvgs.length),
      value: entry ? `${entry.active_fvgs.length} offen` : '-',
    },
    {
      key: 'mss',
      label: de.analysis.mss,
      explanation: de.glossary.mss,
      ok: Boolean(entry?.last_mss),
      value: entry?.last_mss ? entry.last_mss.type : de.analysis.none,
    },
  ];

  const complete = checks.every((c) => c.ok);
  const missing = checks.filter((c) => !c.ok).map((c) => c.label);

  return (
    <section className="panel">
      <h2 className="panel__title">{de.analysis.title}</h2>

      <div className={`bias bias--${bias.bias}`}>
        <span className="bias__label">{de.analysis.htfBias}</span>
        <span className="bias__value">{BIAS_LABEL[bias.bias] ?? bias.bias}</span>
        <span className="bias__score">
          {bias.score >= 0 ? '+' : ''}
          {bias.score.toFixed(3)}
        </span>
      </div>

      <div className="tf-grid">
        {bias.per_timeframe.map((item) => (
          <div key={item.timeframe} className="tf-grid__row">
            <span className="tf-grid__tf">{item.timeframe}</span>
            <span className={`tf-grid__state tf-grid__state--${item.structure_state}`}>
              {item.structure_state}
            </span>
            <span className="tf-grid__score">
              {item.score >= 0 ? '+' : ''}
              {item.score.toFixed(2)}
            </span>
            <span className="tf-grid__detail">
              FVG {item.active_bullish_fvgs}/{item.active_bearish_fvgs}
            </span>
          </div>
        ))}
      </div>

      <h3 className="panel__subtitle">
        {de.analysis.checklist} <span className="muted">({entryTimeframe})</span>
      </h3>

      {entry && !entry.ready ? (
        <p className="notice notice--warn">{de.analysis.notReady}</p>
      ) : (
        <ul className="checklist">
          {checks.map((check) => (
            <li key={check.key} className={check.ok ? 'checklist__ok' : 'checklist__missing'}>
              <span className="checklist__mark">{check.ok ? '✓' : '✗'}</span>
              <span className="checklist__label">
                <Term label={check.label} explanation={check.explanation} />
              </span>
              <span className="checklist__value">{check.value}</span>
            </li>
          ))}
        </ul>
      )}

      <div className={`verdict ${complete ? 'verdict--ok' : 'verdict--none'}`}>
        {complete ? (
          <>
            <strong>Alle geprueften Bedingungen erfuellt</strong>
            <p>{de.analysis.phaseHint}</p>
          </>
        ) : (
          <>
            <strong>{de.analysis.noTrade}</strong>
            <p>
              Es fehlt: <em>{missing.join(', ')}</em>
            </p>
            <p className="muted">{de.analysis.noTradeHint}</p>
          </>
        )}
      </div>

      <h3 className="panel__subtitle">{de.analysis.explain}</h3>
      <ul className="reasons">
        {bias.reasons.map((reason, index) => (
          <li key={`${reason.code}-${index}`} className={reason.ok ? 'reasons__ok' : ''}>
            <span className="reasons__mark">{reason.ok ? '✓' : '·'}</span>
            {translateReason(reason.code, reason.params)}
          </li>
        ))}
      </ul>

      {entry && (
        <dl className="stats">
          <div>
            <dt>
              <Term label={de.analysis.atr} explanation={de.glossary.atr} />
            </dt>
            <dd>{entry.atr === null ? '-' : entry.atr.toFixed(2)}</dd>
          </div>
          <div>
            <dt>{de.analysis.openFvgs}</dt>
            <dd>{entry.active_fvgs.length}</dd>
          </div>
          <div>
            <dt>{de.analysis.untappedPools}</dt>
            <dd>{entry.untapped_pools.length}</dd>
          </div>
          <div>
            <dt>{de.analysis.structure}</dt>
            <dd>{entry.structure_state}</dd>
          </div>
        </dl>
      )}
    </section>
  );
}
