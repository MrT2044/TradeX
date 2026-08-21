/**
 * Farbpalette.
 *
 * Eine Regel bestimmt die Wahl: Zustand muss sich ohne Legende ablesen lassen.
 *   - Kraeftig + durchgezogen = noch aktiv / relevant
 *   - Blass   + gestrichelt   = erledigt (mitigiert, abgelaufen, angetappt)
 * Deshalb hat jede Overlay-Art ein Paar aus "aktiv" und "erledigt".
 */

export const COLORS = {
  chartBg: '#12161d',
  grid: '#1c222c',
  border: '#2a323f',
  textMuted: '#8b95a5',

  candleUp: '#2ea36b',
  candleDown: '#d1494d',

  // Die laufende Bar wird grau gezeichnet: sie ist noch nicht abgeschlossen und
  // damit noch nicht analysiert (Architektur-Invariante 1).
  formingBar: '#3a4250',
  formingBorder: '#5a6577',

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
