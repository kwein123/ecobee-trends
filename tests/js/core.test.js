// Unit tests for app/static/dashboard-core.js. Run: node --test tests/js/
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const C = require("../../app/static/dashboard-core.js");

test("niceTicks covers the range with round steps", () => {
  assert.deepEqual(C.niceTicks(61.3, 78.9, 5), [65, 70, 75]);
  assert.deepEqual(C.niceTicks(0, 100, 5), [0, 20, 40, 60, 80, 100]);
  const flat = C.niceTicks(70, 70, 5);          // a flat line still gets ticks
  assert.ok(flat.length >= 2 && flat[0] < 70 && flat.at(-1) > 70);
  assert.equal(C.niceStep(0), 1);
  assert.equal(C.niceStep(NaN), 1);
});

test("medianInterval ignores outliers and handles tiny inputs", () => {
  assert.equal(C.medianInterval([]), 300e3);
  assert.equal(C.medianInterval([5]), 300e3);
  assert.equal(C.medianInterval([0, 300, 600, 900, 99999]), 300);
});

test("seriesPath breaks at nulls and at time gaps; steps setpoints", () => {
  const id = (v) => v;
  assert.equal(C.seriesPath([0, 1, 2], [5, 6, 7], id, id, 10, false), "M0.0,5.0L1.0,6.0L2.0,7.0");
  assert.equal(C.seriesPath([0, 1, 2], [5, null, 7], id, id, 10, false), "M0.0,5.0M2.0,7.0");
  assert.equal(C.seriesPath([0, 1, 50], [5, 6, 7], id, id, 10, false), "M0.0,5.0L1.0,6.0M50.0,7.0");
  assert.equal(C.seriesPath([0, 1], [5, 6], id, id, 10, true), "M0.0,5.0L1.0,5.0L1.0,6.0");
  assert.equal(C.seriesPath([0, 1], [null, null], id, id, 10, false), "");
});

test("stripRuns merges on-runs, splits at gaps, keeps brief cycles visible", () => {
  const ts = [0, 10, 20, 30, 40, 100, 110];
  const runs = C.stripRuns(ts, [1, 1, 0, 1, 1, 1, 0], 15, 5, 0, 200);
  assert.deepEqual(runs, [
    { start: 0, end: 15, level: 1 },    // clipped at t0
    { start: 25, end: 45, level: 1 },   // 30–40 widened by half a reading
    { start: 95, end: 105, level: 1 },  // after the gap: its own run
  ]);
  // Fractions round up to quarters, so a 5% duty cycle still draws.
  const frac = C.stripRuns([0, 10, 20], [0.05, 0.05, 0.6], 15, 5, 0, 100);
  assert.deepEqual(frac.map((r) => r.level), [0.25, 0.75]);
  assert.deepEqual(C.stripRuns([0, 10], [0, null], 15, 5, 0, 100), []);
});

test("applySavedOrder: saved first, new units after in server order, junk ignored", () => {
  const list = ["a", "b", "c", "d"].map((identifier) => ({ identifier }));
  const ids = (xs) => xs.map((x) => x.identifier);
  assert.deepEqual(ids(C.applySavedOrder(list, ["c", "a"])), ["c", "a", "b", "d"]);
  assert.deepEqual(ids(C.applySavedOrder(list, null)), ["a", "b", "c", "d"]);
  assert.deepEqual(ids(C.applySavedOrder(list, "nonsense")), ["a", "b", "c", "d"]);
  assert.deepEqual(ids(C.applySavedOrder(list, [42, "d", {}, "zzz"])), ["d", "a", "b", "c"]);
  assert.deepEqual(ids(list), ["a", "b", "c", "d"], "input must not be mutated");
});

test("dropIndex places the dragged panel by its center", () => {
  const mids = [100, 300, 500];
  assert.equal(C.dropIndex(50, mids), 0);
  assert.equal(C.dropIndex(299, mids), 1);
  assert.equal(C.dropIndex(301, mids), 2);
  assert.equal(C.dropIndex(9999, mids), 3);
  assert.equal(C.dropIndex(0, []), 0);
});

test("moveBy moves one place and clamps at the ends", () => {
  assert.deepEqual(C.moveBy(["a", "b", "c"], 0, 1), ["b", "a", "c"]);
  assert.deepEqual(C.moveBy(["a", "b", "c"], 2, -1), ["a", "c", "b"]);
  assert.deepEqual(C.moveBy(["a", "b", "c"], 0, -1), ["a", "b", "c"]);
  assert.deepEqual(C.moveBy(["a", "b", "c"], 2, 1), ["a", "b", "c"]);
  assert.deepEqual(C.moveBy(["a", "b", "c"], 5, 1), ["a", "b", "c"]);
});

test("autoScrollSpeed: zero in the middle, faster toward the edges", () => {
  assert.equal(C.autoScrollSpeed(400, 800), 0);
  assert.ok(C.autoScrollSpeed(10, 800) < C.autoScrollSpeed(50, 800));
  assert.ok(C.autoScrollSpeed(10, 800) < 0);
  assert.ok(C.autoScrollSpeed(795, 800) > C.autoScrollSpeed(750, 800));
  assert.equal(C.autoScrollSpeed(-50, 800), -16);   // finger above the viewport
});

test("labels and formatting", () => {
  assert.equal(C.modeLabel("auxHeatOnly"), "Emergency heat");
  assert.equal(C.modeLabel("cool"), "Cool");
  assert.equal(C.modeLabel(null), "Unknown");
  assert.equal(C.modeLabel("newMode"), "newMode");
  assert.equal(C.climateLabel("sleep"), "Sleep");
  assert.equal(C.climateLabel("smart1"), "smart1");
  assert.equal(C.climateLabel(null), "");
  assert.equal(C.fmtTemp(72.25), "72.3°F");
  assert.equal(C.fmtTemp(null), "–");
  assert.equal(C.fmtHum(47.6), "48%");
  assert.equal(C.fmtDuration(900), "15 min");
  assert.equal(C.fmtDuration(3600), "hour");
  assert.equal(C.fmtDuration(21600), "6 h");
  assert.equal(C.fmtDuration(86400), "day");
  assert.equal(C.fmtAgo(30e3), "just now");
  assert.equal(C.fmtAgo(45 * 60e3), "45 min ago");
  assert.equal(C.fmtAgo(3 * 3600e3), "3 h ago");
  assert.equal(C.fmtAgo(72 * 3600e3), "3 days ago");
  assert.equal(C.plural(1, "thermostat"), "1 thermostat");
  assert.equal(C.plural(1200, "reading"), "1,200 readings");
});

test("isStale", () => {
  const now = 10 * 3600e3;
  assert.equal(C.isStale(now - 5 * 60e3, now), false);
  assert.equal(C.isStale(now - 31 * 60e3, now), true);
  assert.equal(C.isStale(null, now), true);
});
