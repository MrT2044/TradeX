/**
 * Laufender Sitzungszustand ueber Server-Sent Events.
 *
 * Warum mit Rueckfall auf Abfragen
 * ---------------------------------
 * SSE laeuft ueber gewoehnliches HTTP und der Browser verbindet nach einem
 * Abriss von selbst neu - genau richtig fuer ein Handy, das das Netz wechselt.
 * Aber es gibt Zwischenstationen, die Datenstroeme puffern oder abschneiden;
 * dahinter wuerde die Anzeige stehenbleiben, ohne dass etwas nach einem Fehler
 * aussieht. Das ist der gefaehrlichste Zustand einer Ueberwachungsansicht:
 * veraltete Zahlen, die wie aktuelle aussehen.
 *
 * Deshalb zwei Dinge:
 *
 *   1. Kommt ueber den Strom eine Weile nichts (auch kein Heartbeat), wird auf
 *      Abfragen umgeschaltet.
 *   2. `stale` sagt der Oberflaeche, wie alt der angezeigte Stand ist. Eine
 *      Ansicht, die das verschweigt, luegt im Stoerfall.
 */

import { useEffect, useRef, useState } from 'react';

import { api } from './client';
import type { SessionStatus } from './types';

/** Ohne Lebenszeichen laenger als das gilt der Strom als tot. Der Server
 *  sendet spaetestens alle 10 s ein `heartbeat`-Ereignis; das Doppelte davon
 *  laesst einen ausgefallenen Takt zu, ohne gleich Alarm zu schlagen. */
const SILENCE_LIMIT_MS = 25_000;

/** Takt des Rueckfalls. Bewusst langsamer als der Strom - er ist der Notnagel,
 *  nicht der Normalfall. */
const POLL_INTERVAL_MS = 3_000;

export type StreamMode = 'stream' | 'polling' | 'offline';

export interface SessionStream {
  status: SessionStatus | null;
  mode: StreamMode;
  /** Sekunden seit der letzten Meldung. Grundlage jeder Altersanzeige. */
  ageSeconds: number;
}

export function useSessionStream(): SessionStream {
  const [status, setStatus] = useState<SessionStatus | null>(null);
  const [mode, setMode] = useState<StreamMode>('offline');
  const [ageSeconds, setAgeSeconds] = useState(0);

  const lastMessage = useRef<number>(Date.now());

  useEffect(() => {
    let cancelled = false;
    let source: EventSource | null = null;
    let pollTimer: number | null = null;

    const markiere = () => {
      lastMessage.current = Date.now();
      setAgeSeconds(0);
    };

    const uebernehmen = (next: SessionStatus) => {
      if (cancelled) return;
      markiere();
      setStatus(next);
    };

    // --- Rueckfall: gewoehnliche Abfragen ---------------------------------
    const startePolling = () => {
      if (cancelled || pollTimer !== null) return;
      setMode('polling');
      const tick = async () => {
        try {
          uebernehmen(await api.session());
        } catch {
          if (!cancelled) setMode('offline');
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

    // --- Normalfall: Strom -------------------------------------------------
    const startStream = () => {
      try {
        source = new EventSource('/api/stream');
      } catch {
        startePolling();
        return;
      }

      source.addEventListener('session', (event) => {
        try {
          uebernehmen(JSON.parse((event as MessageEvent<string>).data) as SessionStatus);
          stopPolling();
          setMode('stream');
        } catch {
          /* Eine unlesbare Meldung ist kein Grund, den Strom aufzugeben -
             die naechste kann wieder in Ordnung sein. */
        }
      });

      // Lebenszeichen ohne Zustandsaenderung. Ohne dieses Ereignis waere ein
      // ruhiges System von einer toten Leitung nicht zu unterscheiden - die
      // Ansicht wuerde nach kurzer Zeit faelschlich "veraltet" melden.
      source.addEventListener('heartbeat', () => {
        markiere();
        if (!cancelled) {
          stopPolling();
          setMode('stream');
        }
      });

      // Der Browser verbindet von selbst neu. Hier wird deshalb NICHT
      // geschlossen, sondern nur der Zustand vermerkt - bis die Stille zu
      // lang wird und der Rueckfall greift.
      source.addEventListener('error', () => {
        // Funktional statt ueber `mode`: der Effekt laeuft genau einmal, also
        // hielte diese Funktion fuer immer den Wert vom ersten Rendern fest
        // ('offline'). Die Bedingung war damit immer wahr, und ein einzelner
        // Aussetzer schrieb "offline", obwohl der Rueckfall auf Abfragen
        // laengst lief und Zahlen lieferte.
        if (!cancelled) setMode((vorher) => (vorher === 'polling' ? vorher : 'offline'));
      });

      source.addEventListener('open', () => {
        if (!cancelled) markiere();
      });
    };

    startStream();

    // --- Stilleueberwachung -------------------------------------------------
    // Der einzige Zeuge dafuer, dass der Strom noch lebt. Ohne sie waere ein
    // abgeschnittener Strom von einem ruhigen System nicht zu unterscheiden.
    const watchdog = window.setInterval(() => {
      if (cancelled) return;
      const still = Date.now() - lastMessage.current;
      setAgeSeconds(Math.floor(still / 1000));
      if (still > SILENCE_LIMIT_MS) startePolling();
    }, 1000);

    return () => {
      cancelled = true;
      window.clearInterval(watchdog);
      stopPolling();
      source?.close();
    };
    // Absichtlich einmalig: der Strom soll die Lebensdauer der Ansicht haben.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return { status, mode, ageSeconds };
}
