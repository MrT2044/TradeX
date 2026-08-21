/**
 * Wiedergabesteuerung.
 *
 * Der eigentliche Zweck von Phase 2: Bar fuer Bar vorwaerts gehen und sehen,
 * WANN welcher Detektor anspringt. Nur so lassen sich die Schwellenwerte gegen
 * echte Kursverlaeufe pruefen, bevor eine Strategie darauf aufsetzt.
 *
 * Wichtig: Es wird derselbe Engine-Aufruf benutzt, den spaeter der Live-Feed
 * macht. Es gibt keinen gesonderten Wiedergabe-Pfad (Architektur-Invariante 3).
 */

import { de } from '../i18n/de';

interface Props {
  cursor: number;
  total: number;
  playing: boolean;
  busy: boolean;
  stepSize: number;
  onStepSize: (value: number) => void;
  onStep: () => void;
  onPlayPause: () => void;
  onReset: () => void;
  onToEnd: () => void;
}

const STEP_SIZES = [1, 5, 15, 60, 240];

export function ReplayControls({
  cursor,
  total,
  playing,
  busy,
  stepSize,
  onStepSize,
  onStep,
  onPlayPause,
  onReset,
  onToEnd,
}: Props) {
  const progress = total ? (cursor / total) * 100 : 0;
  const exhausted = cursor >= total;

  return (
    <section className="replay">
      <div className="replay__head">
        <h2 className="panel__title">{de.replay.title}</h2>
        <span className="muted">{de.replay.hint}</span>
      </div>

      <div className="replay__bar">
        <div className="replay__progress">
          <div className="replay__progress-fill" style={{ width: `${progress}%` }} />
        </div>
        <span className="replay__counter">
          {cursor.toLocaleString('de-DE')} {de.common.of} {total.toLocaleString('de-DE')}
        </span>
      </div>

      <div className="replay__controls">
        <button type="button" onClick={onReset} disabled={busy || cursor === 0}>
          {de.replay.reset}
        </button>
        <button type="button" onClick={onStep} disabled={busy || exhausted}>
          +{stepSize} {de.replay.step}
        </button>
        <button
          type="button"
          className={playing ? 'primary' : ''}
          onClick={onPlayPause}
          disabled={busy || exhausted}
        >
          {playing ? de.replay.pause : de.replay.play}
        </button>
        <button type="button" onClick={onToEnd} disabled={busy || exhausted}>
          {de.replay.toEnd}
        </button>

        <label className="field field--inline">
          <span className="field__label">{de.replay.speed}</span>
          <select
            className="field__input"
            value={stepSize}
            onChange={(event) => onStepSize(Number(event.target.value))}
          >
            {STEP_SIZES.map((size) => (
              <option key={size} value={size}>
                {size} Bars
              </option>
            ))}
          </select>
        </label>
      </div>
    </section>
  );
}
