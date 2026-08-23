/** Fehlerband mit Kopierknopf.
 *
 * Eine Fehlermeldung nuetzt nur, wenn sie irgendwo ankommt. Abtippen von einem
 * Bildschirm ist fehleranfaellig, und markieren mit der Maus scheitert an einem
 * Band, das beim naechsten Zustandswechsel verschwindet. Ein Knopf, der den
 * Wortlaut in die Zwischenablage legt, kostet zwanzig Zeilen und spart jedes
 * Mal eine Rueckfrage.
 *
 * Kopiert wird der Wortlaut MIT Zeitstempel: ohne ihn laesst sich eine Meldung
 * spaeter nicht mehr der Zeile im Protokoll zuordnen, die dazu gehoert.
 */

import { useEffect, useState } from 'react';

import { de } from '../i18n/de';

type Zustand = 'bereit' | 'kopiert' | 'gescheitert';

interface Props {
  message: string;
  onDismiss: () => void;
}

export function ErrorBanner({ message, onDismiss }: Props) {
  const [zustand, setZustand] = useState<Zustand>('bereit');

  // Die Rueckmeldung faellt nach kurzer Zeit zurueck - ein dauerhaftes
  // "Kopiert" liesse offen, ob es den letzten oder einen frueheren Klick meint.
  useEffect(() => {
    if (zustand === 'bereit') return;
    const timer = window.setTimeout(() => setZustand('bereit'), 2000);
    return () => window.clearTimeout(timer);
  }, [zustand]);

  const kopieren = async () => {
    const text = `[${new Date().toISOString()}] ${message}`;
    try {
      await navigator.clipboard.writeText(text);
      setZustand('kopiert');
    } catch {
      // Die Zwischenablage kann verweigert werden (kein sicherer Kontext,
      // fehlende Berechtigung). Dann wird das gesagt, statt so zu tun, als
      // waere es gelungen - sonst fuegt jemand eine alte Meldung ein.
      setZustand('gescheitert');
    }
  };

  return (
    <div className="banner banner--error">
      <strong>{de.common.error}:</strong>
      <span className="banner__text">{message}</span>
      <button
        type="button"
        className="banner__copy"
        onClick={() => void kopieren()}
        title={de.common.copy}
      >
        {zustand === 'kopiert'
          ? de.common.copied
          : zustand === 'gescheitert'
            ? de.common.copyFailed
            : de.common.copy}
      </button>
      <button type="button" onClick={onDismiss} title={de.common.dismiss}>
        ×
      </button>
    </div>
  );
}
