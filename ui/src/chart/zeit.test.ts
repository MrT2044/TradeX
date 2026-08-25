/**
 * Sommer- und Winterzeit der Chart-Achse.
 *
 * Der Fehler, gegen den das hier steht: ein fester Aufschlag von zwei Stunden
 * auf UTC. Der ist von Ende Maerz bis Ende Oktober richtig und den Rest des
 * Jahres um eine Stunde daneben - und weil er die Haelfte des Jahres stimmt,
 * faellt er beim Ausprobieren nicht auf.
 *
 * Laeuft ohne Zusatzpaket:  node --test src/chart/zeit.test.ts
 * (Node entfernt die Typannotationen selbst; deshalb steht diese Datei auch
 * nicht im `include` von tsconfig.json - dort fehlten die Node-Typen.)
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { crosshairZeit, tickMark, TickMarkArt } from './zeit.ts';

const BERLIN = 'Europe/Berlin';

/** 15.01.2026, 12:00 UTC - Winterzeit, CET = UTC+1. */
const WINTER = Date.UTC(2026, 0, 15, 12, 0, 0) / 1000;
/** 15.07.2026, 12:00 UTC - Sommerzeit, CEST = UTC+2. */
const SOMMER = Date.UTC(2026, 6, 15, 12, 0, 0) / 1000;

test('Winterzeit: 12:00 UTC ist 13:00 in Berlin', () => {
  assert.equal(tickMark(BERLIN, WINTER, TickMarkArt.Zeit), '13:00');
});

test('Sommerzeit: 12:00 UTC ist 14:00 in Berlin', () => {
  assert.equal(tickMark(BERLIN, SOMMER, TickMarkArt.Zeit), '14:00');
});

test('der Unterschied ist genau eine Stunde - also keine feste Addition', () => {
  const winter = Number(tickMark(BERLIN, WINTER, TickMarkArt.Zeit).slice(0, 2));
  const sommer = Number(tickMark(BERLIN, SOMMER, TickMarkArt.Zeit).slice(0, 2));
  assert.equal(sommer - winter, 1, 'CEST muss eine Stunde weiter sein als CET');
});

test('das Fadenkreuz zeigt denselben Tag in derselben Zone', () => {
  assert.equal(crosshairZeit(BERLIN, SOMMER), '15.07.2026, 14:00');
});

test('nach Mitternacht Ortszeit kippt auch das Datum', () => {
  // 31.12.2026, 23:30 UTC ist in Berlin bereits der 01.01.2027.
  const silvester = Date.UTC(2026, 11, 31, 23, 30, 0) / 1000;
  assert.equal(tickMark(BERLIN, silvester, TickMarkArt.Tag), '01.01.');
});

test('UTC bleibt UTC - der Waechter gegen einen fest verdrahteten Aufschlag', () => {
  assert.equal(tickMark('UTC', SOMMER, TickMarkArt.Zeit), '12:00');
});

test('eine unbekannte Zone laesst den Chart nicht leer', () => {
  assert.equal(tickMark('Nicht/Existent', SOMMER, TickMarkArt.Zeit), '12:00');
});
