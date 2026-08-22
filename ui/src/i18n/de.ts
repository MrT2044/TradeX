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

/** Welche Strategie - siehe tradex/strategy/registry.py. */
export const STRATEGY_LABEL: Record<string, string> = {
  ict_chain: 'Pflichtkette (ICT)',
  opening_range: 'Eroeffnungsspanne',
};

/** Wodurch eine simulierte Position endete - siehe tradex/domain/enums.py. */
export const EXIT_REASON: Record<string, string> = {
  stop: 'Stop',
  target: 'Ziel',
  time: 'Zeitstop',
  end_of_data: 'Datenende',
};

/** Woran der Stop verankert ist - siehe tradex/strategy/stops.py. */
export const STOP_ANCHOR: Record<string, string> = {
  retracement: 'Rücklauf-Tief',
  sweep: 'Sweep-Extrem',
  swing: 'letzter Hoch-/Tiefpunkt',
  fvg: 'Kursluecken-Kante',
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

  'setup.started': (p) => `Setup gestartet nach Sweep bei ${price(p.sweep)}`,
  'setup.complete': (p) =>
    `Alle Pflichtbedingungen erfuellt (Sweep ${price(p.sweep)}, Kursluecke ${num(p.fvg_ticks, 0)} Ticks)`,
  'setup.invalidated_beyond_sweep': (p) =>
    `Kurs ist wieder unter das Sweep-Extrem ${price(p.sweep_extreme)} gefallen - die Grundannahme gilt nicht mehr`,
  'setup.invalidated_bias_flip': (p) =>
    `Die Gesamtrichtung hat auf ${BIAS_WORD[String(p.bias)] ?? p.bias} gedreht`,
  'setup.expired': (p) => `Zeitfenster abgelaufen (${p.age_bars} Bars in Stufe "${p.stage}")`,
  'setup.crowded_out': (p) =>
    `Von neueren Setups verdraengt - hoechstens ${p.limit} gleichzeitig (war in Stufe "${p.stage}")`,

  'opening_range.breakout': (p) =>
    `Ausbruch aus der Eroeffnungsspanne der ersten ${p.minutes} Minuten` +
    ` (${price(p.low)} bis ${price(p.high)}, ${num(p.width_ticks, 0)} Ticks breit)`,

  'stop.placed': (p) =>
    `Stop bei ${price(p.price)} (${num(p.ticks, 0)} Ticks, Anker: ${STOP_ANCHOR[String(p.anchor)] ?? p.anchor})`,
  'stop.too_wide': (p) => `Stop zu weit: ${num(p.ticks, 0)} Ticks, erlaubt sind ${p.max}`,
  'stop.too_tight': (p) =>
    `Stop zu eng: ${num(p.ticks, 0)} Ticks - er laege im normalen Rauschen (mindestens ${p.min})`,

  'target.liquidity': (p) =>
    `Ziel ${price(p.price)} an der naechsten unberuehrten Liquiditaet, CRV ${num(p.rr, 2)}`,
  'target.fallback_r_multiple': (p) =>
    `Kein Liquiditaetsziel in Reichweite - festes Vielfaches, CRV ${num(p.rr, 2)}`,
  'target.none': () => 'Kein brauchbares Ziel gefunden',
  'target.rr_too_low': (p) =>
    `Bestes erreichbares CRV ${num(p.best_rr, 2)} liegt unter dem Minimum ${num(p.min_rr, 1)}`,

  'risk.size_ok': (p) =>
    `${p.quantity} Kontrakt(e), Risiko ${num(p.risk_amount, 2)} USD von ${num(p.risk_budget, 2)} USD`,
  'risk.size_zero': (p) =>
    `Schon ein Kontrakt riskiert ${num(p.risk_per_contract, 2)} USD - mehr als das Budget von ${num(p.risk_budget, 2)} USD (Stop ${num(p.stop_ticks, 0)} Ticks)`,
  'risk.size_capped': (p) => `Stueckzahl auf ${p.quantity} begrenzt (Obergrenze ${p.cap})`,
  'risk.daily_loss_limit': (p) =>
    `Tagesverlust ${num(p.realized_loss, 2)} USD gegen Limit ${num(p.limit, 2)} USD`,
  'risk.max_trades_per_day': (p) => `${p.taken} von ${p.limit} Trades heute bereits genommen`,
  'risk.max_open_positions': (p) => `${p.open} von ${p.limit} Positionen bereits offen`,
  'risk.disabled': () => 'Risikopruefung ist abgeschaltet',

  'window.ok': () => 'Handelsfenster passt',
  'window.session_blocked': (p) =>
    `Session "${p.session}" ist nicht freigegeben (erlaubt: ${Array.isArray(p.allowed) ? p.allowed.join(', ') : '-'})`,
  'window.volatility_low': (p) =>
    `Zu ruhig: ${num(p.atr_ticks, 0)} Ticks Schwankungsbreite, mindestens ${p.min} noetig`,
  'window.volatility_high': (p) =>
    `Zu volatil: ${num(p.atr_ticks, 0)} Ticks Schwankungsbreite, hoechstens ${p.max} erlaubt`,

  'decision.trade': (p) =>
    `${p.side}: Einstieg ${price(p.entry)}, Stop ${price(p.stop)}, Ziel ${price(p.target)}, CRV ${num(p.rr, 2)}, ${p.quantity} Kontrakt(e)`,
  'decision.no_trade': (p) =>
    Array.isArray(p.missing) && p.missing.length
      ? `Kein Trade - es fehlt: ${(p.missing as string[]).join(', ')}`
      : 'Kein Trade',

  'data.warmup': (p) => `Noch zu wenig Historie (${p.bars} von ${p.required} Bars)`,
  'data.gap': (p) => `Luecke in den Daten: ${p.missing_bars} fehlende Bars`,
  'data.roll_boundary': () => 'Kontraktwechsel - Preissprung ist ein Buchungsartefakt',
  'market.closed': () => 'Markt ist geschlossen',
};

export function translateReason(code: string, params: ReasonParams): string {
  const fn = reasonText[code];
  return fn ? fn(params) : code;
}

/**
 * Kurzbezeichnungen ohne Parameter.
 *
 * Fuer die Zaehlansicht ("184x Kurs hinter den Sweep gefallen") gibt es keine
 * Parameter - dort wuerden die ausformulierten Saetze aus `reasonText`
 * "undefined" einsetzen. Deshalb eine eigene, parameterfreie Liste.
 */
export const reasonLabel: Record<string, string> = {
  'setup.invalidated_beyond_sweep': 'Kurs wieder hinter das Sweep-Extrem gefallen',
  'setup.invalidated_bias_flip': 'Gesamtrichtung hat gedreht',
  'setup.expired': 'Zeitfenster abgelaufen',
  'setup.crowded_out': 'Von neueren Setups verdraengt',
  'mss.missing': 'Struktur hat nicht rechtzeitig gedreht',
  'target.rr_too_low': 'Chance-Risiko-Verhaeltnis zu schlecht',
  'target.none': 'Kein brauchbares Ziel',
  'stop.too_wide': 'Stop zu weit',
  'stop.too_tight': 'Stop zu eng',
  'risk.size_zero': 'Stop zu weit fuer das Risikobudget',
  'risk.daily_loss_limit': 'Tagesverlustlimit erreicht',
  'risk.max_trades_per_day': 'Maximale Trades pro Tag erreicht',
  'risk.max_open_positions': 'Maximal offene Positionen erreicht',
  'window.session_blocked': 'Session nicht freigegeben',
  'window.volatility_low': 'Zu geringe Schwankungsbreite',
  'window.volatility_high': 'Zu hohe Schwankungsbreite',
  'decision.no_trade': 'Setup unvollstaendig',
};

export function translateReasonLabel(code: string): string {
  return reasonLabel[code] ?? code;
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
  strategy: {
    title: 'Strategie',
    activeSetups: 'Setups im Aufbau',
    noSetups: 'Zurzeit kein Setup im Aufbau',
    lastSignal: 'Letztes Signal',
    noSignal: 'Noch kein Signal',
    decisions: 'Entscheidungen',
    trades: 'Signale',
    noTrades: 'Ablehnungen',
    whyNoTrade: 'Warum wird nicht gehandelt?',
    entry: 'Einstieg',
    stop: 'Stop',
    target: 'Ziel',
    rr: 'CRV',
    quantity: 'Kontrakte',
    risk: 'Risiko',
    reward: 'Chance',
    stopAnchor: 'Stop-Anker',
    setupTf: 'Setup-Ebene',
    confirmTf: 'Bestaetigung',
    minRr: 'Mindest-CRV',
    notExecuted:
      'Signale werden berechnet und protokolliert, aber nicht ausgefuehrt. Ausfuehrung kommt ab Phase 5.',
    stages: {
      swept: 'Liquiditaet geholt',
      displaced: 'Impuls + Kursluecke',
      retraced: 'Ruecklauf erfolgt',
      confirmed: 'bestaetigt',
      invalidated: 'ungueltig',
      expired: 'verfallen',
    } as Record<string, string>,
    conditions: {
      sweep: 'Liquidity Sweep',
      displacement: 'Impuls',
      fvg: 'Kursluecke',
      retracement: 'Ruecklauf',
      mss: 'Strukturbruch',
    } as Record<string, string>,
  },
  backtest: {
    title: 'Backtest',
    hint: 'Rechnet die Regeln ueber den gesamten geladenen Zeitraum durch und simuliert, was aus jedem Signal geworden waere.',
    run: 'Backtest rechnen',
    running: 'Rechnet ...',
    never: 'Fuer dieses Instrument wurde noch kein Backtest gerechnet.',
    readFirst: 'Zuerst lesen',
    notSignificant: 'Zu kleine Stichprobe - die Zahlen unten sind noch kein Befund.',
    period: 'Zeitraum',
    result: 'Ergebnis',
    trades: 'Trades',
    winRate: 'Trefferquote',
    expectancy: 'Erwartungswert',
    expectancyHint:
      'Durchschnittliches Ergebnis je Trade, gemessen in R (Vielfaches des eingegangenen Risikos). Ueber 0 heisst: die Regel traegt sich - vorausgesetzt, die Stichprobe ist gross genug.',
    profitFactor: 'Profitfaktor',
    profitFactorHint: 'Summe der Gewinne geteilt durch Summe der Verluste. Unter 1 verliert das System.',
    payoff: 'Gewinn/Verlust',
    sqn: 'SQN',
    sqnHint:
      'Erwartungswert im Verhaeltnis zu seiner Schwankung und zur Anzahl der Trades. Ein guter Schnitt aus acht Trades bekommt hier keine gute Note.',
    netPnl: 'Nettoergebnis',
    commission: 'davon Gebuehren',
    equity: 'Kontoverlauf',
    finalEquity: 'Konto am Ende',
    returnPct: 'Rendite',
    drawdown: 'Groesster Rueckgang',
    drawdownHint: 'Groesster Rueckgang vom jeweiligen Hoechststand - nicht vom Startwert.',
    lossStreak: 'Laengste Verlustserie',
    holding: 'Haltedauer',
    mae: 'MAE / MFE',
    maeHint:
      'Wie weit lief es gegen die Position (MAE) und wie weit fuer sie (MFE), jeweils in R. Beantwortet, ob ein engerer Stop ueberhaupt haltbar gewesen waere.',
    halves: 'Zeitraumhaelften',
    halvesHint:
      'Erste und zweite Haelfte getrennt gerechnet. Weichen sie stark ab, haengt das Ergebnis eher am Zeitraum als an der Regel.',
    firstHalf: 'erste Haelfte',
    secondHalf: 'zweite Haelfte',
    byStrategy: 'Nach Strategie',
    byStrategyHint:
      'Mehrere Strategien laufen parallel am selben Konto. Diese Aufstellung zeigt, welche das Ergebnis getragen hat - und welche nur Gebuehren produziert.',
    bySession: 'Nach Session',
    byDirection: 'Nach Richtung',
    byExit: 'Nach Ausstiegsart',
    signals: 'Signale',
    filled: 'gefuellt',
    unfilled: 'nie gefuellt',
    stale: 'ueber Datenluecke verworfen',
    assumptions: 'Annahmen der Ausfuehrung',
    assumptionsHint:
      'Ein Ergebnis ohne seine Ausfuehrungsannahmen ist nicht interpretierbar. Sie stehen in config/default.yaml unter "backtest".',
    whyNoTrade: 'Warum kein Trade zustande kam',
    noTrades:
      'Kein einziger Trade. Bevor die Kennzahlen etwas bedeuten koennen, muss geklaert sein, woran die Kette scheitert.',
    trade: 'Trade',
    exitReason: 'Ausstieg',
    rMultiple: 'R',
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
