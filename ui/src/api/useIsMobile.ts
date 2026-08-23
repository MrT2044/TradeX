/**
 * Ist der Bildschirm ein Telefon?
 *
 * Bewusst in JavaScript und nicht nur per CSS: die beiden Ansichten sollen
 * nicht beide im Baum haengen und sich gegenseitig ausblenden. Das Chart mit
 * seinen Overlays wuerde sonst auf dem Telefon mitgeladen und mitgerechnet -
 * unsichtbar, aber teuer, und ausgerechnet auf dem Geraet mit der knappsten
 * Rechenzeit und dem teuersten Datenvolumen.
 *
 * Der Umbruch liegt bei 820 px: darunter ist eine Chartansicht mit
 * Seitenleiste nicht mehr sinnvoll bedienbar. Ein iPhone liegt im
 * Hochformat weit darunter, im Querformat darueber - das ist gewollt, quer
 * kann man tatsaechlich ein Chart lesen.
 */

import { useEffect, useState } from 'react';

export const MOBILE_BREAKPOINT_PX = 820;

const QUERY = `(max-width: ${MOBILE_BREAKPOINT_PX}px)`;

export function useIsMobile(): boolean {
  const [isMobile, setIsMobile] = useState(
    () => typeof window !== 'undefined' && window.matchMedia(QUERY).matches,
  );

  useEffect(() => {
    const liste = window.matchMedia(QUERY);
    const merke = (event: MediaQueryListEvent) => setIsMobile(event.matches);
    liste.addEventListener('change', merke);
    // Zwischen erstem Rendern und diesem Effekt kann sich die Groesse bereits
    // geaendert haben - etwa beim Drehen waehrend des Ladens.
    setIsMobile(liste.matches);
    return () => liste.removeEventListener('change', merke);
  }, []);

  return isMobile;
}
