/* dashboard-core.js — the dashboard's pure logic: scales, paths, ordering,
   formatting. No DOM access, so it runs unchanged under Node for the unit
   tests in tests/js/. The page loads it before dashboard.js. */
(function (root) {
  "use strict";

  /* ---------- scales ---------- */

  function niceStep(rough) {
    if (!(rough > 0)) return 1;
    const pow = Math.pow(10, Math.floor(Math.log10(rough)));
    for (const m of [1, 2, 2.5, 5, 10]) if (rough <= m * pow) return m * pow;
    return 10 * pow;
  }

  function niceTicks(min, max, count) {
    if (!(max > min)) { min -= 1; max += 1; }
    const step = niceStep((max - min) / Math.max(count, 1));
    const ticks = [];
    for (let v = Math.ceil(min / step) * step; v <= max + 1e-9; v += step)
      ticks.push(Math.round(v * 100) / 100);
    return ticks;
  }

  /* Typical spacing between readings (ms); 5 minutes if there's too little data. */
  function medianInterval(ts) {
    if (ts.length < 2) return 300e3;
    const d = [];
    for (let i = 1; i < ts.length; i++) d.push(ts[i] - ts[i - 1]);
    d.sort((a, b) => a - b);
    return d[Math.floor(d.length / 2)];
  }

  /* SVG path for one series. Breaks the line at missing values and at time
     gaps longer than gapMs (logger downtime stays visible as a gap); `step`
     draws setpoints as steps rather than slopes. */
  function seriesPath(ts, values, x, y, gapMs, step) {
    let d = "", pen = false, prevT = null, prevV = null;
    for (let i = 0; i < ts.length; i++) {
      const v = values[i];
      if (v == null) { pen = false; prevT = null; continue; }
      if (prevT != null && ts[i] - prevT > gapMs) pen = false;
      const px = x(ts[i]).toFixed(1), py = y(v).toFixed(1);
      if (!pen) { d += `M${px},${py}`; pen = true; }
      else if (step) d += `L${px},${y(prevV).toFixed(1)}L${px},${py}`;
      else d += `L${px},${py}`;
      prevT = ts[i]; prevV = v;
    }
    return d;
  }

  /* Equipment strip as runs of equal intensity. Values are 0/1 for single
     readings, or the fraction of readings a bucket had the equipment on.
     Fractions are rounded up to quarters so brief cycles stay visible.
     Returns [{start, end, level}] in ms, each run widened by half a reading
     on either side and clipped to [t0, t1]. */
  function stripRuns(ts, values, gapMs, half, t0, t1) {
    const runs = [];
    let cur = null;
    for (let i = 0; i < ts.length; i++) {
      const v = values[i] || 0;
      const level = v >= 1 ? 1 : v > 0 ? Math.ceil(v * 4) / 4 : 0;
      const continues = cur && level === cur.level && ts[i] - cur.lastT <= gapMs;
      if (!continues && cur) { runs.push(cur); cur = null; }
      if (level > 0) {
        if (!cur) cur = { start: ts[i], lastT: ts[i], level };
        else cur.lastT = ts[i];
      }
    }
    if (cur) runs.push(cur);
    return runs.map((r) => ({
      start: Math.max(r.start - half, t0),
      end: Math.min(r.lastT + half, t1),
      level: r.level,
    })).filter((r) => r.end > r.start);
  }

  /* ---------- panel order ---------- */

  /* Panels in the saved order; anything not in it (a new thermostat) keeps
     the server's order, after the saved ones. `saved` may be anything read
     back from storage, so it is validated rather than trusted. */
  function applySavedOrder(list, saved) {
    const ids = Array.isArray(saved) ? saved.filter((s) => typeof s === "string") : [];
    const pos = new Map(ids.map((id, i) => [id, i]));
    return list
      .map((item, i) => ({ item, i }))
      .sort((a, b) =>
        (pos.get(a.item.identifier) ?? 1e9) - (pos.get(b.item.identifier) ?? 1e9) || a.i - b.i)
      .map((e) => e.item);
  }

  /* Where a dragged panel belongs: the index among the *other* panels
     before which it goes, judged by its center against their midpoints. */
  function dropIndex(draggedCenterY, otherMidpoints) {
    for (let i = 0; i < otherMidpoints.length; i++)
      if (draggedCenterY < otherMidpoints[i]) return i;
    return otherMidpoints.length;
  }

  /* Move ids[from] by `delta` places, clamped to the ends. Returns a copy. */
  function moveBy(ids, from, delta) {
    const out = ids.slice();
    const to = Math.max(0, Math.min(out.length - 1, from + delta));
    if (from < 0 || from >= out.length || to === from) return out;
    const [item] = out.splice(from, 1);
    out.splice(to, 0, item);
    return out;
  }

  /* Edge auto-scroll speed (px per frame) for a pointer at y in a viewport
     of height h: faster the closer to the edge, zero outside the zone. */
  function autoScrollSpeed(y, h, zone = 64, max = 16) {
    if (y < zone) return -Math.ceil(max * Math.min(1, (zone - y) / zone));
    if (y > h - zone) return Math.ceil(max * Math.min(1, (y - (h - zone)) / zone));
    return 0;
  }

  /* ---------- text ---------- */

  const MODE_LABELS = {
    heat: "Heat", cool: "Cool", auto: "Auto (heat or cool)",
    auxHeatOnly: "Emergency heat", off: "Off",
  };
  function modeLabel(mode) {
    return MODE_LABELS[mode] || (mode ? String(mode) : "Unknown");
  }

  /* ecobee's built-in comfort settings are "home", "away", "sleep"; custom
     ones have generated refs (e.g. "smart1") that are better left as-is. */
  function climateLabel(ref) {
    if (!ref) return "";
    if (["home", "away", "sleep"].includes(ref)) return ref[0].toUpperCase() + ref.slice(1);
    return ref;
  }

  function fmtTemp(v) { return v == null ? "–" : v.toFixed(1) + "°F"; }
  function fmtHum(v) { return v == null ? "–" : Math.round(v) + "%"; }

  function fmtDuration(sec) {
    if (sec < 3600) return `${Math.round(sec / 60)} min`;
    if (sec < 86400) { const h = sec / 3600; return h === 1 ? "hour" : `${+h.toFixed(1)} h`; }
    const d = sec / 86400;
    return d === 1 ? "day" : `${+d.toFixed(1)} days`;
  }

  function fmtAgo(ms) {
    const min = Math.floor(ms / 60e3);
    if (min < 1) return "just now";
    if (min < 60) return `${min} min ago`;
    const h = Math.round(min / 60);
    if (h < 48) return `${h} h ago`;
    return `${Math.round(h / 24)} days ago`;
  }

  function plural(n, word) { return `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`; }

  /* A reading older than this means the logger (or ecobee) has stopped. */
  const STALE_MS = 30 * 60e3;
  function isStale(latestMs, nowMs) { return latestMs == null || nowMs - latestMs > STALE_MS; }

  const api = {
    niceStep, niceTicks, medianInterval, seriesPath, stripRuns,
    applySavedOrder, dropIndex, moveBy, autoScrollSpeed,
    modeLabel, climateLabel, fmtTemp, fmtHum, fmtDuration, fmtAgo, plural,
    isStale, STALE_MS,
  };
  root.EcobeeCore = api;
  if (typeof module === "object" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
