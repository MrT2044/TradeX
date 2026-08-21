/**
 * Deutsche Oberflaechentexte.
 *
 * Die Engine liefert Reason-Codes plus Parameter, nie fertige Saetze. Erst hier
 * werden daraus lesbare Begruendungen. Das haelt Begruendungen testbar und
 * mehrsprachig (Spec 23, 25).
 *
 * Fachbegriffe bleiben englisch - so heissen sie in jeder Charting-Software.
 * Erklaert werden sie im Glossar unten und als Tooltip in der Oberflaeche.
 */

export type ReasonParams = Record<string, unknown>;

const num = (value: unknown, digits = 2): string =>
  typeof value === 'number' ? value.toFixed(digits) : String(value ?? '-');

const price = (value: unknown): string =>
  typeof value === 'number' ? value.toLocaleString('de-DE', { maximumFractionDigits: 2 }) : '-';

const BIAS_WORD: Record<string, string> = {
  bullish: 'steigend',
  bearish: 'fallend',
  neutral: 'richtungslos',
};

const STATE_WORD: Record<string, string> = {
  bullish: 'aufwaerts',
  bearish: 'abwaerts',
  range: 'seitwaerts',
};

/** Uebersetzt einen Reason-Code der Engine in einen deutschen Satz. */
export const reasonText: Record<string, (p: ReasonParams) => string> = {
  'htf.bias': (p) =>
    `Gesamtrichtung der hoeheren Timeframes: ${BIAS_WORD[String(p.bias)] ?? p.bias}` +
    ` (Wert ${num(p.score, 3)}, neutral bis ±${num(p.neutral_band, 2)})`,
  'htf.structure': (p) =>
    `${p.timeframe}: Marktstruktur laeuft ${STATE_WORD[String(p.state)] ?? p.state}`,
  'htf.fvg_balance': (p) =>
    `${p.timeframe}: ${p.bullish} offene Kursluecken nach oben, ${p.bearish} nach unten`,
  'htf.liquidity_draw': (p) =>
    `${p.timeframe}: naechstes unberuehrtes Level oben ${price(p.buy_side)}, unten ${price(p.sell_side)}`,

  'sweep.found': (p) =>
    `Liquiditaet bei ${price(p.price)} wurde geholt und der Kurs kam zurueck` +
    ` (${num(p.depth_ticks, 1)} Ticks tief, nach ${p.bars_to_reclaim} Bars zurueck)`,
  'sweep.missing': () => 'Es wurde keine Liquiditaet geholt',

  'displacement.found': (p) =>
    `Impulskerze: ${num(p.range_atr_mult, 2)}-fache normale Schwankungsbreite,` +
    ` Koerperanteil ${num(Number(p.body_ratio) * 100, 0)} %`,
  'displacement.missing': () => 'Kein ausreichend starker Impuls',
  'displacement.volume_confirmed': (p) =>
    `Volumen ${num(p.volume_ratio, 1)}-fach ueber dem Durchschnitt`,

  'fvg.found': (p) => `Kursluecke von ${num(p.size_ticks, 0)} Ticks entstanden`,
  'fvg.missing': () => 'Der Impuls hat keine gueltige Kursluecke hinterlassen',

  'retracement.found': (p) => `Kurs ist in die Kursluecke zurueckgelaufen (${price(p.price)})`,
  'retracement.missing': () => 'Kurs ist noch nicht in die Kursluecke zurueckgelaufen',

  'mss.found': (p) => `Struktur auf ${p.timeframe} hat gedreht (${p.type})`,
  'mss.missing': () => 'Die Struktur hat noch nicht gedreht',

  'data.warmup': (p) => `Noch zu wenig Historie (${p.bars} von ${p.required} Bars)`,
  'data.gap': (p) => `Luecke in den Daten: ${p.missing_bars} fehlende Bars`,
  'data.roll_boundary': () => 'Kontraktwechsel - Preissprung ist ein Buchungsartefakt',
  'market.closed': () => 'Markt ist geschlossen',
};

export function translateReason(code: string, params: ReasonParams): string {
  const fn = reasonText[code];
  return fn ? fn(params) : code;
}

export const de = {
  app: {
    title: 'TradeX',
    subtitle: 'Nasdaq-100 Futures - regelbasierte Analyse',
  },
  status: {
    symbol: 'Instrument',
    market: 'Markt',
    open: 'OFFEN',
    closed: 'GESCHLOSSEN',
    mode: 'Modus',
    dataFeed: 'Datenquelle',
    session: 'Session',
    bars: 'Bars',
    modes: {
      analysis_only: 'Nur Analyse',
      paper_manual: 'Paper, manuell',
      paper_auto: 'Paper, automatisch',
      live_manual: 'Live, manuell',
      live_auto: 'Live, automatisch',
    } as Record<string, string>,
    sessions: {
      asia: 'Asien',
      london: 'London',
      ny_am: 'New York (Vormittag)',
      ny_pm: 'New York (Nachmittag)',
      closed: 'Geschlossen',
    } as Record<string, string>,
  },
  chart: {
    timeframe: 'Zeitebene',
    loading: 'Lade Kursdaten ...',
    empty: 'Keine Daten geladen',
    legend: 'Legende',
    showFvg: 'Kursluecken (FVG)',
    showLiquidity: 'Liquiditaet',
    showSwings: 'Hoch-/Tiefpunkte',
    showStructure: 'Struktur (BOS/MSS)',
    showSweeps: 'Sweeps',
    forming: 'laufende Bar',
  },
  analysis: {
    title: 'Analyse',
    htfBias: 'Richtung (4H + 1H)',
    checklist: 'Setup-Pruefung',
    explain: 'Warum sieht der Bot das so?',
    structure: 'Marktstruktur',
    liquidity: 'Liquiditaet',
    displacement: 'Impuls',
    fvg: 'Kursluecke',
    retracement: 'Ruecklauf',
    mss: 'Strukturbruch',
    noTrade: 'KEIN SETUP',
    noTradeHint:
      'Es fehlt mindestens eine Pflichtbedingung. Der Bot ergaenzt fehlende Bedingungen nicht.',
    phaseHint:
      'Phase 2: Es werden nur Muster erkannt und angezeigt. Einstiege, Stops und Ziele kommen in Phase 3.',
    notReady: 'Noch nicht genug Historie fuer eine belastbare Aussage',
    atr: 'Schwankungsbreite (ATR)',
    openFvgs: 'offene Kursluecken',
    untappedPools: 'unberuehrte Level',
    lastMss: 'letzter Strukturbruch',
    lastDisplacement: 'letzter Impuls',
    none: 'keiner',
  },
  replay: {
    title: 'Wiedergabe',
    hint: 'Bar fuer Bar vorwaerts gehen und beobachten, wann welcher Detektor anspringt.',
    step: 'Schritt',
    play: 'Abspielen',
    pause: 'Pause',
    reset: 'Zuruecksetzen',
    toEnd: 'Ans Ende',
    progress: 'Fortschritt',
    speed: 'Tempo',
  },
  system: {
    title: 'System',
    health: 'Systemzustand',
    providers: 'Datenquellen',
    integrity: 'Datenqualitaet',
    clean: 'Keine Auffaelligkeiten',
    gaps: 'Luecken',
    missingBars: 'fehlende Bars',
    invalidBars: 'ungueltige Bars',
    configHash: 'Konfiguration',
    strategyVersion: 'Strategieversion',
    capabilities: 'Faehigkeiten',
    logs: 'Protokoll',
    noLogs: 'Noch keine Eintraege',
  },
  data: {
    title: 'Daten',
    noData: 'Keine Daten vorhanden',
    noDataHint:
      'Es liegen noch keine Kursdaten im lokalen Speicher. Synthetische Testdaten erzeugen:',
    noDataCommand: 'python scripts/generate_demo_data.py --days 45',
    realDataHint: 'Echte MNQ-Historie ueber Databento laden (Kostenvoranschlag wird vorher gezeigt):',
    realDataCommand: 'python scripts/fetch_databento.py --symbol MNQ --from 2023-01-01',
    coverage: 'Verfuegbar',
    load: 'Laden',
    loading: 'Lade ...',
  },
  glossary: {
    fvg: 'Fair Value Gap (Kursluecke): Ein Preisbereich, den der Markt in einer schnellen Bewegung uebersprungen hat. Solche Bereiche werden oft spaeter noch einmal angelaufen.',
    liquidity:
      'Liquiditaet: Kursniveaus, an denen sich Stop-Orders sammeln - typischerweise knapp ueber vorherigen Hochs und unter vorherigen Tiefs.',
    sweep:
      'Sweep: Der Kurs sticht kurz durch so ein Niveau (holt die Stops) und kehrt sofort wieder zurueck. Das unterscheidet ihn vom echten Ausbruch, bei dem der Kurs drueben bleibt.',
    displacement:
      'Displacement (Impuls): Eine ungewoehnlich grosse Kerze mit grossem Koerper - ein Zeichen fuer entschlossene Bewegung statt Rauschen.',
    mss: 'Market Structure Shift: Der erste Strukturbruch GEGEN die bisherige Richtung. Gilt als Hinweis, dass die Richtung gedreht hat.',
    bos: 'Break of Structure: Ein Strukturbruch IN Richtung des bestehenden Trends - also eine Fortsetzung.',
    atr: 'ATR: Durchschnittliche Schwankungsbreite der letzten Kerzen. Dient als Massstab dafuer, was gerade "gross" ist.',
    htfBias:
      'Higher Timeframe Bias: Die Richtung der grossen Zeitebenen (4H und 1H). Sie entscheidet, in welche Richtung ueberhaupt nach Setups gesucht wird.',
  },
  common: {
    yes: 'ja',
    no: 'nein',
    unknown: 'unbekannt',
    error: 'Fehler',
    retry: 'Erneut versuchen',
    of: 'von',
  },
};

export type Translations = typeof de;
