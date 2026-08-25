/**
 * Einstellungen, die einen Neustart ueberleben.
 *
 * Bewusst `localStorage` und nicht die Engine: das hier sind Anzeigevorlieben
 * eines Betrachters, keine Betriebsparameter. Sie gehoeren nicht in
 * `default.yaml` - was dort steht, geht in den `config_hash` ein und damit in
 * jeden Protokolleintrag und jeden gespeicherten Backtest-Lauf. Ein
 * eingeschaltetes FVG-Overlay wuerde dann zwei Laeufe unvergleichbar machen,
 * obwohl sich an der Analyse nichts geaendert hat.
 */

import { useEffect, useState } from 'react';

const PRAEFIX = 'tradex.';

/** Wie `useState`, nur dass der Wert unter `schluessel` erhalten bleibt.
 *
 *  Alle Zugriffe sind abgesichert: im privaten Modus mancher Browser wirft
 *  schon das Lesen von `localStorage`. Eine Oberflaeche, die daran nicht
 *  startet, waere ein hoher Preis fuer eine gespeicherte Vorliebe.
 */
export function useGespeicherteEinstellung<T>(
  schluessel: string,
  standard: T,
  /** Prueft, ob ein gelesener Wert brauchbar ist. Ohne diese Pruefung reisst
   *  ein alter oder von Hand veraenderter Eintrag die Ansicht ab - und zwar
   *  bei jedem Start erneut, bis jemand den Speicher leert. */
  istGueltig: (wert: unknown) => wert is T,
): [T, (wert: T | ((vorher: T) => T)) => void] {
  const [wert, setWert] = useState<T>(() => {
    try {
      const roh = window.localStorage.getItem(PRAEFIX + schluessel);
      if (roh === null) return standard;
      const gelesen: unknown = JSON.parse(roh);
      return istGueltig(gelesen) ? gelesen : standard;
    } catch {
      return standard;
    }
  });

  useEffect(() => {
    try {
      window.localStorage.setItem(PRAEFIX + schluessel, JSON.stringify(wert));
    } catch {
      /* Kein Speicherplatz oder kein Zugriff - die Sitzung laeuft trotzdem
         weiter, nur eben ohne Gedaechtnis. */
    }
  }, [schluessel, wert]);

  return [wert, setWert];
}
