/**
 * Zeitachse und Fadenkreuz in der konfigurierten Zeitzone.
 *
 * Das Problem, das hier geloest wird: die Engine rechnet durchgehend in UTC,
 * `lightweight-charts` bekommt Sekunden seit Epoch und zeigt sie ohne weiteres
 * Zutun als UTC an. Um 14:20 Berliner Zeit stand deshalb 12:20 im Chart - und
 * das faellt nur auf, wenn man gerade hinsieht und den Markt kennt.
 *
 * Zwei Zeilen daneben waeren zwei Stunden Aufschlag. Genau das ist die falsche
 * Loesung: sie ist von Ende Maerz bis Ende Oktober richtig und den Rest des
 * Jahres um eine Stunde daneben. `Intl.DateTimeFormat` mit einer IANA-Zone
 * kennt die Umstellung; eine Konstante kann sie nicht kennen.
 *
 * Intern aendert sich nichts: die Zeitstempel bleiben UTC, lokalisiert wird
 * ausschliesslich, was auf dem Bildschirm steht.
 */

/** Was `lightweight-charts` an `tickMarkFormatter` uebergibt. Die Bibliothek
 *  fuehrt die Werte als Aufzaehlung; hier zaehlt nur die Reihenfolge
 *  Jahr < Monat < Tag < Zeit. */
export const TickMarkArt = {
  Jahr: 0,
  Monat: 1,
  Tag: 2,
  Zeit: 3,
  ZeitMitSekunden: 4,
} as const;

function formatiere(
  timeZone: string,
  optionen: Intl.DateTimeFormatOptions,
  sekunden: number,
): string {
  try {
    return new Intl.DateTimeFormat('de-DE', { ...optionen, timeZone }).format(
      new Date(sekunden * 1000),
    );
  } catch {
    // Eine unbekannte Zonenkennung darf den Chart nicht leerlassen. Dann eben
    // UTC - falsch, aber sichtbar falsch und nicht leer.
    return new Intl.DateTimeFormat('de-DE', { ...optionen, timeZone: 'UTC' }).format(
      new Date(sekunden * 1000),
    );
  }
}

/** Beschriftung eines Achsenstrichs. Je groeber das Raster, desto weniger
 *  Stellen - eine Achse voller vollstaendiger Zeitstempel ist unlesbar. */
export function tickMark(timeZone: string, sekunden: number, art: number): string {
  if (art <= TickMarkArt.Jahr) return formatiere(timeZone, { year: 'numeric' }, sekunden);
  if (art === TickMarkArt.Monat) {
    return formatiere(timeZone, { month: 'short', year: '2-digit' }, sekunden);
  }
  if (art === TickMarkArt.Tag) {
    return formatiere(timeZone, { day: '2-digit', month: '2-digit' }, sekunden);
  }
  if (art >= TickMarkArt.ZeitMitSekunden) {
    return formatiere(
      timeZone,
      { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false },
      sekunden,
    );
  }
  return formatiere(timeZone, { hour: '2-digit', minute: '2-digit', hour12: false }, sekunden);
}

/** Beschriftung im Fadenkreuz und in der Legende - hier darf es vollstaendig
 *  sein, denn es steht nur an einer Stelle. */
export function crosshairZeit(timeZone: string, sekunden: number): string {
  return formatiere(
    timeZone,
    {
      day: '2-digit',
      month: '2-digit',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    },
    sekunden,
  );
}
