/**
 * Laufender Kurs und laufende Kerze ueber Server-Sent Events.
 *
 * Warum ein eigener Strom
 * -----------------------
 * `useSessionStream` traegt den gesamten Betriebszustand und prueft einmal je
 * Sekunde. Fuer eine Kursanzeige ist das zu grob - so sah der Chart neben
 * NinjaTrader aus, als stuende er still. Und die Kurse dorthin zu legen waere
 * die falsche Richtung: dann ginge jede Sekunde der ganze Betriebszustand
 * ueber die Leitung, weil sich eine Zahl darin geaendert hat.
 *
 * Warum Push und nicht schneller abfragen
 * ---------------------------------------
 * Abfragen alle 250 ms koennen hoechstens vier sichtbare Aktualisierungen je
 * Sekunde liefern, und jede kostet eine HTTP-Anfrage. Der Server sendet
 * stattdessen, sobald sich etwas aendert - und fasst dabei zusammen: geschickt
 * wird der NEUESTE Kurs, nicht jeder einzelne Tick. Fuer eine Anzeige ist ein
 * verpasster Zwischenkurs folgenlos, ein verpasster letzter nicht.
 *
 * Rueckfall auf Abfragen, aus demselben Grund wie beim Sitzungsstrom: hinter
 * einer puffernden Zwischenstation bliebe die Anzeige sonst stehen, ohne dass
 * etwas nach einem Fehler aussieht.
 */

import { useEffect, useRef, useState } from 'react';

import { api } from './client';
import type { Bar, TickEvent } from './types';

/** Ohne Lebenszeichen laenger als das gilt der Strom als tot. Der Server
 *  sendet spaetestens alle 5 s ein `heartbeat`-Ereignis. */
const SILENCE_LIMIT_MS = 12_000;

/** Takt des Rueckfalls. Der Notnagel darf langsamer sein als der Normalfall,
 *  aber nicht so langsam, dass die Kerze sichtbar springt. */
const POLL_INTERVAL_MS = 500;

export interface TickStream {
  /** Zuletzt gehandelter Kurs - 0, wenn keiner bekannt ist. */
  price: number;
  /** Die laufende Kerze, fertig gebucketet vom Server. Null heisst: keine. */
  bar: Bar | null;
}

export function useTickStream(symbol: string, timeframe: string, aktiv: boolean): TickStream {
  const [price, setPrice] = useState(0);
  const [bar, setBar] = useState<Bar | null>(null);
  const lastMessage = useRef<number>(Date.now());

  useEffect(() => {
    // Beim Wechsel von Symbol oder Zeitebene gehoert der alte Kurs zu etwas
    // anderem. Ihn stehenzulassen waere die schlimmste Sorte Anzeige: nicht
    // falsch beschriftet, sondern schlicht ein anderer Markt.
    setPrice(0);
    setBar(null);
    if (!aktiv || !symbol) return;

    let cancelled = false;
    let source: EventSource | null = null;
    let pollTimer: number | null = null;

    const uebernehmen = (next: TickEvent) => {
      if (cancelled) return;
      // Eine Meldung, die zu einem anderen Symbol oder einer anderen Zeitebene
      // gehoert, wird verworfen statt gezeichnet - beim Umschalten koennen
      // beide Strome kurz nebeneinander liegen.
      if (next.symbol !== symbol.toUpperCase() || next.timeframe !== timeframe) return;
      lastMessage.current = Date.now();
      setPrice(next.price);
      setBar(next.bar);
    };

    const startePolling = () => {
      if (cancelled || pollTimer !== null) return;
      const tick = async () => {
        try {
          const daten = await api.bars(symbol, timeframe, 1);
          if (cancelled) return;
          lastMessage.current = Date.now();
          setBar(daten.live);
          setPrice(daten.live?.close ?? 0);
        } catch {
          /* Ein Aussetzer ist kein Grund aufzugeben. */
        }
      };
      void tick();
      pollTimer = window.setInterval(() => void tick(), POLL_INTERVAL_MS);
    };

    const stopPolling = () => {
      if (pollTimer !== null) {
        window.clearInterval(pollTimer);
        pollTimer = null;
      }
    };

    const pfad = `/api/ticks?symbol=${encodeURIComponent(symbol)}&timeframe=${timeframe}`;
    try {
      source = new EventSource(pfad);
    } catch {
      startePolling();
    }

    source?.addEventListener('tick', (event) => {
      try {
        uebernehmen(JSON.parse((event as MessageEvent<string>).data) as TickEvent);
        stopPolling();
      } catch {
        /* Eine unlesbare Meldung ist kein Grund, den Strom aufzugeben. */
      }
    });

    source?.addEventListener('heartbeat', () => {
      lastMessage.current = Date.now();
      stopPolling();
    });

    source?.addEventListener('open', () => {
      lastMessage.current = Date.now();
    });

    const watchdog = window.setInterval(() => {
      if (cancelled) return;
      if (Date.now() - lastMessage.current > SILENCE_LIMIT_MS) startePolling();
    }, 1000);

    return () => {
      cancelled = true;
      window.clearInterval(watchdog);
      stopPolling();
      source?.close();
    };
  }, [symbol, timeframe, aktiv]);

  return { price, bar };
}
