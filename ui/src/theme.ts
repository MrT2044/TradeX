/**
 * Farbpalette.
 *
 * Eine Regel bestimmt die Wahl: Zustand muss sich ohne Legende ablesen lassen.
 *   - Kraeftig + durchgezogen = noch aktiv / relevant
 *   - Blass   + gestrichelt   = erledigt (mitigiert, abgelaufen, angetappt)
 * Deshalb hat jede Overlay-Art ein Paar aus "aktiv" und "erledigt".
 */

export const COLORS = {
  // Nur die Chartflaeche ist schwarz, nicht die Anwendung: die Kerzen sollen
  // den staerksten Kontrast im Bild haben. Das uebrige Theme bleibt unberuehrt
  // (`--bg-panel` in styles.css).
  chartBg: '#000000',
  grid: '#161a21',
  border: '#2a323f',
  textMuted: '#8b95a5',

  candleUp: '#16c784',
  candleDown: '#ea3943',
  //: Nur bei exakt open == close. Eine Kerze ohne Richtung rot oder gruen zu
  //  faerben waere eine Aussage, die die Bar nicht hergibt.
  candleFlat: '#8b95a5',

  fvgBull: {
    fill: 'rgba(46, 163, 107, 0.16)',
    border: 'rgba(46, 163, 107, 0.55)',
    fillDone: 'rgba(46, 163, 107, 0.05)',
    borderDone: 'rgba(46, 163, 107, 0.22)',
  },
  fvgBear: {
    fill: 'rgba(209, 73, 77, 0.16)',
    border: 'rgba(209, 73, 77, 0.55)',
    fillDone: 'rgba(209, 73, 77, 0.05)',
    borderDone: 'rgba(209, 73, 77, 0.22)',
  },

  liquidityBuy: 'rgba(232, 176, 68, 0.85)',
  liquiditySell: 'rgba(106, 164, 232, 0.85)',
  liquidityTapped: 'rgba(120, 130, 145, 0.35)',

  swing: 'rgba(139, 149, 165, 0.7)',
  bos: '#5b8fd6',
  mss: '#e0a53c',
  sweep: '#b06ad4',
} as const;
