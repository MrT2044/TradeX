/**
 * Kurschart mit Analyse-Overlays.
 *
 * Das Chart rechnet NICHTS selbst nach. Es zeichnet ausschliesslich, was die
 * Engine geliefert hat (Spec 27). Damit ist ausgeschlossen, dass Anzeige und
 * Analyse jemals auseinanderlaufen.
 */

import {
  CandlestickSeries,
  createChart,
  createSeriesMarkers,
  type CandlestickData,
  type IChartApi,
  type ISeriesApi,
  type SeriesMarker,
  type Time,
} from 'lightweight-charts';
import { useEffect, useMemo, useRef } from 'react';

import type { Bar, BarsResponse, Overlays } from '../api/types';
import { COLORS } from '../theme';
import { OverlayPrimitive, type OverlayBox, type OverlayLine } from './primitives/overlay';
import { crosshairZeit, tickMark } from './zeit';

export interface ChartToggles {
  fvg: boolean;
  liquidity: boolean;
  swings: boolean;
  structure: boolean;
  sweeps: boolean;
}

interface Props {
  bars: BarsResponse | null;
  overlays: Overlays | null;
  toggles: ChartToggles;
  priceDecimals: number;
  /** Die Kerze, die sich gerade bildet - aus Ticks gebaut, vom Server im
   *  richtigen Bucket geliefert. Sie geht in keine Analyse ein (Invariante 1).
   *  Kommt haeufiger herein als `bars` und bewegt nur diese eine Kerze. */
  liveBar?: Bar | null;
  /** IANA-Zeitzone der Achsenbeschriftung. Intern bleibt alles UTC. */
  timeZone: string;
}

/** Engine-Timestamps sind Nanosekunden UTC, lightweight-charts erwartet Sekunden. */
const toChartTime = (ts: number): Time => Math.floor(ts / 1_000_000_000) as Time;

/** Richtungsfarbe einer Kerze - dieselbe Regel wie in jedem Handelschart.
 *
 *  Sie wird bei JEDEM Tick neu bestimmt, nicht einmal beim Anlegen: laeuft der
 *  Kurs innerhalb derselben Kerze ueber den Eroeffnungspreis, muss sie sofort
 *  von rot auf gruen umschlagen. Neutral gilt ausschliesslich bei exakt
 *  open == close - jede andere Faerbung behauptete eine Richtung, die die Bar
 *  nicht hat. */
const kerzenFarbe = (bar: Bar): string =>
  bar.close > bar.open ? COLORS.candleUp : bar.close < bar.open ? COLORS.candleDown : COLORS.candleFlat;

/** Eine Kerze fuer die Bibliothek - geschlossen wie laufend, gleiche Regel.
 *
 *  Die laufende war frueher grau, um zu zeigen, dass sie NICHT analysiert wurde. Das
 *  hat den Zweck verfehlt: grau sah nach kaputt aus, und die Richtung - die
 *  eigentliche Auskunft einer Kerze - fehlte. Dass sie nicht analysiert ist,
 *  steht ohnehin an der Anzeige `BEOBACHTUNG`/`ECHTZEIT` daneben, und die
 *  Invariante haengt nicht an einer Farbe, sondern daran, welchen Weg die Bar
 *  nimmt. */
const kerze = (bar: Bar): CandlestickData<Time> => {
  const farbe = kerzenFarbe(bar);
  return {
    time: toChartTime(bar.ts),
    open: bar.open,
    high: bar.high,
    low: bar.low,
    close: bar.close,
    color: farbe,
    borderColor: farbe,
    wickColor: farbe,
  };
};

function buildBoxes(overlays: Overlays | null, toggles: ChartToggles): OverlayBox[] {
  if (!overlays || !toggles.fvg) return [];

  return overlays.fvgs.map((zone) => {
    const bullish = zone.direction === 'bullish';
    const done = zone.state === 'mitigated' || zone.state === 'expired';
    // Erledigte Zonen bleiben sichtbar, aber blass und gestrichelt: fuer die
    // visuelle Pruefung der Schwellenwerte muss man auch sehen, was der
    // Detektor frueher gefunden und der Markt dann abgearbeitet hat.
    const base = bullish ? COLORS.fvgBull : COLORS.fvgBear;
    return {
      from: toChartTime(zone.created_ts) as unknown as number,
      // Aktive Zonen laufen bis zum rechten Rand - sie sind weiterhin relevant.
      // Erledigte enden dort, wo der Markt sie abgearbeitet hat. Wuerde man sie
      // ebenfalls durchziehen, waere das Chart nach kurzer Zeit voller Altlasten.
      to: done && zone.closed_ts ? (toChartTime(zone.closed_ts) as unknown as number) : null,
      top: zone.top,
      bottom: zone.bottom,
      fill: done ? base.fillDone : base.fill,
      border: done ? base.borderDone : base.border,
      dashed: done,
    };
  });
}

function buildLines(overlays: Overlays | null, toggles: ChartToggles): OverlayLine[] {
  if (!overlays || !toggles.liquidity) return [];

  return overlays.pools.map((pool) => {
    const untapped = pool.state === 'untapped';
    const buySide = pool.side === 'buy_side';
    return {
      from: toChartTime(pool.created_ts) as unknown as number,
      to: null,
      price: pool.price,
      color: untapped
        ? buySide
          ? COLORS.liquidityBuy
          : COLORS.liquiditySell
        : COLORS.liquidityTapped,
      // Staerke = Anzahl der Swings im Cluster. Dickere Linie bedeutet mehr
      // aufgestaute Orders an diesem Niveau.
      width: Math.min(1 + Math.max(pool.strength - 1, 0), 3),
      dashed: !untapped,
      label: pool.kind === 'swing' ? undefined : pool.label,
    };
  });
}

function buildMarkers(overlays: Overlays | null, toggles: ChartToggles): SeriesMarker<Time>[] {
  if (!overlays) return [];
  const markers: SeriesMarker<Time>[] = [];

  if (toggles.swings) {
    for (const swing of overlays.swings) {
      const high = swing.type === 'swing_high';
      markers.push({
        time: toChartTime(swing.ts),
        position: high ? 'aboveBar' : 'belowBar',
        color: COLORS.swing,
        shape: high ? 'arrowDown' : 'arrowUp',
        size: 0.5,
      });
    }
  }

  // Beschriftungen sparsam einsetzen: auf einem Chart mit hunderten Ereignissen
  // verdecken Textmarker die Kurse, die sie erklaeren sollen. Text bekommt nur
  // der MSS - die Bedingung, an der ein Setup steht oder faellt (Spec 7).
  if (toggles.structure) {
    for (const event of overlays.structure_events) {
      const bullish = event.type.endsWith('bullish');
      const isMss = event.type.startsWith('mss');
      markers.push({
        time: toChartTime(event.ts),
        position: bullish ? 'belowBar' : 'aboveBar',
        color: isMss ? COLORS.mss : COLORS.bos,
        shape: 'circle',
        size: isMss ? 1 : 0.6,
        ...(isMss ? { text: 'MSS' } : {}),
      });
    }
  }

  if (toggles.sweeps) {
    for (const sweep of overlays.sweeps) {
      const bullish = sweep.direction === 'bullish';
      markers.push({
        time: toChartTime(sweep.reclaim_ts),
        position: bullish ? 'belowBar' : 'aboveBar',
        color: COLORS.sweep,
        shape: bullish ? 'arrowUp' : 'arrowDown',
        size: 0.8,
      });
    }
  }

  // Mehrere Marker auf derselben Bar sind erlaubt, aber die Liste MUSS zeitlich
  // aufsteigend sein - sonst zeichnet die Bibliothek sie nicht.
  markers.sort((a, b) => (a.time as number) - (b.time as number));
  return markers;
}

export function TradeChart({
  bars,
  overlays,
  toggles,
  priceDecimals,
  liveBar,
  timeZone,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<'Candlestick', Time> | null>(null);
  const primitiveRef = useRef<OverlayPrimitive | null>(null);
  const markersRef = useRef<ReturnType<typeof createSeriesMarkers<Time>> | null>(null);
  /** Welcher Datenbestand gerade gezeichnet ist - Symbol und Zeitebene.
   *  Wechselt er, wird die Ansicht neu eingepasst; sonst bleibt sie stehen. */
  const datensatzRef = useRef<string>('');

  // Chart einmalig anlegen.
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const chart = createChart(container, {
      layout: {
        background: { color: COLORS.chartBg },
        textColor: COLORS.textMuted,
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: COLORS.grid },
        horzLines: { color: COLORS.grid },
      },
      rightPriceScale: { borderColor: COLORS.border },
      timeScale: {
        borderColor: COLORS.border,
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 8,
      },
      crosshair: { mode: 0 },
      autoSize: true,
    });

    const series = chart.addSeries(CandlestickSeries, {
      upColor: COLORS.candleUp,
      downColor: COLORS.candleDown,
      borderUpColor: COLORS.candleUp,
      borderDownColor: COLORS.candleDown,
      wickUpColor: COLORS.candleUp,
      wickDownColor: COLORS.candleDown,
    });

    const primitive = new OverlayPrimitive();
    series.attachPrimitive(primitive);

    chartRef.current = chart;
    seriesRef.current = series;
    primitiveRef.current = primitive;
    markersRef.current = createSeriesMarkers(series, []);

    return () => {
      markersRef.current = null;
      primitiveRef.current = null;
      seriesRef.current = null;
      chartRef.current = null;
      chart.remove();
    };
  }, []);

  useEffect(() => {
    seriesRef.current?.applyOptions({
      priceFormat: { type: 'price', precision: priceDecimals, minMove: 0.25 },
    });
  }, [priceDecimals]);

  // Zeitachse und Fadenkreuz in der konfigurierten Zone.
  //
  // Ohne diese beiden Formatierer zeigt lightweight-charts die Sekunden als
  // UTC - im Chart stand 12:20, wo um 14:20 gehandelt wurde. Die Zone kommt
  // aus der Konfiguration und nicht aus dem Browser: massgeblich ist, in
  // welcher Zeit dieses System rechnet, nicht wo gerade jemand sitzt. Ein
  // fester Aufschlag waere die Haelfte des Jahres falsch (siehe zeit.ts).
  useEffect(() => {
    chartRef.current?.applyOptions({
      localization: { timeFormatter: (time: Time) => crosshairZeit(timeZone, time as number) },
      timeScale: {
        tickMarkFormatter: (time: Time, art: number) => tickMark(timeZone, time as number, art),
      },
    });
  }, [timeZone]);

  // Kerzen setzen. Die laufende Bar wird angehaengt -
  // sie ist noch nicht abgeschlossen und wurde deshalb NICHT analysiert.
  useEffect(() => {
    const series = seriesRef.current;
    if (!series || !bars) return;

    // Jede Kerze bekommt ihre Farbe ausdruecklich, auch die geschlossenen.
    // Sonst entschiede die Bibliothek fuer open == close von sich aus auf
    // "auf" - und dieselbe Bar saehe je nachdem, ob sie gerade laeuft oder
    // schon fertig ist, verschieden aus.
    const data: CandlestickData<Time>[] = bars.bars.map((bar) => kerze(bar));

    // Zwei verschiedene Dinge, und die Reihenfolge entscheidet:
    //
    //   `forming` ist der Zwischenstand aus GESCHLOSSENEN Basis-Bars. Auf 1m
    //   ist das die zuletzt fertige Minute - also um 14:21:36 die von 14:20.
    //   `live` ist die Kerze, die GERADE entsteht, aus Ticks.
    //
    // Liegen beide im selben Bucket (5m und groesser), ist `live` der
    // vollstaendigere Stand und ersetzt `forming`. Auf 1m liegen sie in
    // verschiedenen Minuten und gehoeren beide ins Bild - genau hier hing der
    // Chart vorher eine Minute hinterher.
    const zeigeForming = bars.forming && (!bars.live || bars.live.ts !== bars.forming.ts);
    if (zeigeForming && bars.forming) data.push(kerze(bars.forming));
    if (bars.live) data.push(kerze(bars.live));

    series.setData(data);

    // Nur bei einem WIRKLICH neuen Datenbestand einpassen - nicht bei jeder
    // Aktualisierung. Vorher sprang der Chart im Echtbetrieb alle fuenf
    // Sekunden auf die Gesamtansicht zurueck, und man konnte nicht
    // hineinzoomen, ohne sofort wieder herausgerissen zu werden. Wer den
    // Ausschnitt gewaehlt hat, will ihn behalten; die Ansicht gehoert dem
    // Betrachter, nicht dem Aktualisierungstakt.
    const kennung = `${bars.symbol}-${bars.timeframe}`;
    if (kennung !== datensatzRef.current) {
      datensatzRef.current = kennung;
      chartRef.current?.timeScale().fitContent();
    }
  }, [bars]);

  // Laufende Kerze nachfuehren.
  //
  // Bars kommen im Minutentakt, Kurse mehrmals je Sekunde. Ohne das hier stand
  // die Kerze bis zu einer Minute still, waehrend NinjaTrader daneben lief -
  // und ein Chart, der sich nicht ruehrt, sieht kaputt aus.
  //
  // `series.update` fasst genau EINE Kerze an und laesst Ausschnitt und Zoom
  // in Ruhe (`fitContent` steht bewusst nur oben, beim Wechsel des
  // Datenbestands). Der Bucket kommt fertig vom Server: wuerde er hier
  // gerechnet, gaebe es zwei Bucket-Raster nebeneinander, und an der
  // Minutengrenze zwei Kerzen fuer dieselbe Minute.
  //
  // Angefasst wird ausschliesslich die laufende Kerze. Sie ist per Invariante
  // 1 von der Analyse ausgenommen; keine geschlossene Bar wird nachtraeglich
  // veraendert.
  useEffect(() => {
    const series = seriesRef.current;
    if (!series || !liveBar) return;
    // Nicht rueckwaerts zeichnen: waehrend eines Symbolwechsels kann ein
    // Kurs des vorigen Datenbestands ankommen, und `update` mit einem
    // aelteren Zeitstempel wirft.
    const letzte = bars?.bars.at(-1);
    if (letzte && liveBar.ts <= letzte.ts) return;
    series.update(kerze(liveBar));
  }, [liveBar, bars]);

  const boxes = useMemo(() => buildBoxes(overlays, toggles), [overlays, toggles]);
  const lines = useMemo(() => buildLines(overlays, toggles), [overlays, toggles]);
  const markers = useMemo(() => buildMarkers(overlays, toggles), [overlays, toggles]);

  useEffect(() => {
    primitiveRef.current?.setData(boxes, lines);
  }, [boxes, lines]);

  useEffect(() => {
    markersRef.current?.setMarkers(markers);
  }, [markers]);

  return <div className="chart-canvas" ref={containerRef} />;
}
