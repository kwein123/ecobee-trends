/* dashboard.js — renders the Ecobee Trends page. Pure helpers (scales,
   ordering, formatting) live in dashboard-core.js, loaded first. */
(function () {
"use strict";
const C = window.EcobeeCore;

/* ---------- configuration ---------- */

const RANGES = [
  { label: "6h", hours: 6, text: "the last 6 hours" },
  { label: "24h", hours: 24, text: "the last 24 hours" },
  { label: "48h", hours: 48, text: "the last 48 hours" },
  { label: "7d", hours: 168, text: "the last 7 days" },
  { label: "30d", hours: 720, text: "the last 30 days" },
  { label: "All", hours: 0, text: "all recorded history" },
];

const TEMP_SERIES = [
  { key: "actual_temp_f",  label: "Indoor temp",   color: "--s-blue" },
  { key: "desired_heat_f", label: "Heat setpoint", color: "--s-red",  step: true, dash: true },
  { key: "desired_cool_f", label: "Cool setpoint", color: "--s-aqua", step: true, dash: true },
  { key: "outdoor_temp_f", label: "Outdoor temp",  color: "--s-yellow" },
];
const SENSOR_COLORS = ["--s-violet", "--s-magenta", "--s-green", "--s-orange"];

/* Humidity shares the chart with temperature, so its hues must not repeat
   the temperature hues — each entity keeps its own color. */
const HUM_SERIES = [
  { key: "actual_humidity",    label: "Indoor %RH",       color: "--s-violet" },
  { key: "desired_dehumidity", label: "Dehumidify above", color: "--s-orange",  step: true, dash: true },
  { key: "desired_humidity",   label: "Humidify below",   color: "--s-magenta", step: true, dash: true },
  { key: "outdoor_humidity",   label: "Outdoor %RH",      color: "--s-green" },
];

const EQUIP_META = {
  compCool1:  { label: "Cooling",         color: "--s-aqua" },
  compCool2:  { label: "Cooling (stg 2)", color: "--s-aqua" },
  heatPump:   { label: "Heat pump",       color: "--s-red" },
  heatPump2:  { label: "Heat pump 2",     color: "--s-red" },
  heatPump3:  { label: "Heat pump 3",     color: "--s-red" },
  auxHeat1:   { label: "Heat",            color: "--s-red" },
  auxHeat2:   { label: "Heat (stg 2)",    color: "--s-red" },
  auxHeat3:   { label: "Heat (stg 3)",    color: "--s-red" },
  fan:        { label: "Fan",             color: "--s-violet" },
  humidifier: { label: "Humidifier",      color: "--s-magenta" },
  dehumidifier: { label: "Dehumidifier",  color: "--s-orange" },
  ventilator: { label: "Ventilator",      color: "--s-yellow" },
  economizer: { label: "Economizer",      color: "--s-yellow" },
};
const eqMeta = (tok) => EQUIP_META[tok] || { label: tok, color: "--s-green" };

const REFRESH_MS = 5 * 60e3;
const ORDER_KEY = "ecobee-viz:panel-order";

/* ---------- storage ---------- */

/* localStorage can be missing or throw (private browsing, blocked site
   data). Preferences then last for this visit instead of breaking the page. */
const store = (() => {
  const mem = new Map();
  let ls = null;
  try {
    ls = window.localStorage;
    ls.setItem("ecobee-viz:probe", "1");
    ls.removeItem("ecobee-viz:probe");
  } catch { ls = null; }
  return {
    get(key) {
      if (ls) { try { return ls.getItem(key); } catch { /* fall through */ } }
      return mem.has(key) ? mem.get(key) : null;
    },
    set(key, value) {
      mem.set(key, value);
      if (ls) { try { ls.setItem(key, value); } catch { /* memory copy stands */ } }
    },
  };
})();

const visKey = (ident, series) => `ecobee-viz:${ident}:${series}`;
const isVisible = (ident, series) => store.get(visKey(ident, series)) !== "0";
const setVisible = (ident, series, on) => store.set(visKey(ident, series), on ? "1" : "0");

/* ---------- state ---------- */

const state = {
  hours: 24,
  data: null,
  fetchedAt: 0,
  controller: null,     // AbortController of the request in flight
  refreshTimer: null,
  drag: null,           // the panel move in progress, if any
  pendingRender: false, // a render that waited for a drag to finish
  width: 0,             // last rendered width; only width changes re-render
  openTables: new Set(),
  tips: new Set(),      // tooltip hide() functions
};

const $ = (sel, el = document) => el.querySelector(sel);
const panelsRoot = () => $("#panels");

function announce(text) {
  const el = $("#announcer");
  el.textContent = "";
  requestAnimationFrame(() => { el.textContent = text; });
}

/* ---------- formatting (locale-aware, so not in core) ---------- */

function fmtTick(ms, spanMs) {
  const d = new Date(ms);
  if (spanMs <= 36 * 3600e3)
    return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (spanMs <= 10 * 86400e3)
    return d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " +
           d.toLocaleTimeString([], { hour: "numeric" });
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}
function fmtFull(ms) {
  return new Date(ms).toLocaleString([], {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
  });
}
function fmtClock(ms) {
  return new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}
function eqText(tok, v) {
  const label = eqMeta(tok).label;
  return v >= 1 ? label : `${label} ${Math.round(v * 100)}%`;
}

/* ---------- chart ---------- */

const SVG_NS = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs, parent) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (parent) parent.appendChild(el);
  return el;
}

/* Narrow screens get narrower axis gutters so the plot keeps its width. */
const marginsFor = (width) => (width < 480 ? { left: 36, right: 36 } : { left: 46, right: 46 });

function drawCombined(container, spec) {
  // One chart per thermostat: °F scale on the left, % scale on the right,
  // equipment on/off strips below the plot, shared x axis at the bottom.
  const width = container.clientWidth;
  const M = spec.margin;
  const strips = spec.eqStrips.filter((s) => s.visible);
  const TOP = 8, PLOT_H = width < 480 ? 220 : 260, ROW = 26, XAXIS = 22;
  const eqH = strips.length ? strips.length * ROW + 6 : 0;
  const H = TOP + PLOT_H + eqH + XAXIS;
  const svg = svgEl("svg", {
    viewBox: `0 0 ${width} ${H}`, width, height: H, role: "img",
    "aria-label": spec.label,
  }, container);

  const [t0, t1] = spec.domain;
  const x = (t) => M.left + ((t - t0) / (t1 - t0 || 1)) * (width - M.left - M.right);
  const plotBottom = TOP + PLOT_H;

  const domainOf = (defs) => {
    let lo = Infinity, hi = -Infinity;
    for (const s of defs) {
      if (!s.visible) continue;
      for (const v of s.values) if (v != null) { if (v < lo) lo = v; if (v > hi) hi = v; }
    }
    if (lo === Infinity) return null;
    const pad = (hi - lo) * 0.12 || 1.5;
    const ticks = C.niceTicks(lo - pad, hi + pad, 5);
    return {
      lo: Math.min(lo - pad, ticks[0]),
      hi: Math.max(hi + pad, ticks[ticks.length - 1]),
      ticks,
    };
  };
  const scaleFor = (dom) => (v) => TOP + (1 - (v - dom.lo) / (dom.hi - dom.lo || 1)) * PLOT_H;

  const tDom = domainOf(spec.tempDefs);
  const hDom = domainOf(spec.humDefs);

  // Gridlines follow the left (°F) scale; if no temperature series is
  // visible the % scale supplies them instead (never both).
  const gridDom = tDom || hDom;
  if (gridDom) {
    const gy = scaleFor(gridDom);
    for (const tv of gridDom.ticks)
      svgEl("line", {
        x1: M.left, x2: width - M.right, y1: gy(tv), y2: gy(tv),
        stroke: "var(--grid)", "stroke-width": 1,
      }, svg);
  }
  if (tDom) {
    const yT = scaleFor(tDom);
    for (const tv of tDom.ticks) {
      const lb = svgEl("text", { x: M.left - 6, y: yT(tv) + 3.5, "text-anchor": "end" }, svg);
      lb.textContent = `${tv}°`;
    }
  }
  if (hDom) {
    const yH = scaleFor(hDom);
    for (const tv of hDom.ticks) {
      svgEl("line", {
        x1: width - M.right, x2: width - M.right + 4, y1: yH(tv), y2: yH(tv),
        stroke: "var(--baseline)", "stroke-width": 1,
      }, svg);
      const lb = svgEl("text", { x: width - M.right + 7, y: yH(tv) + 3.5, "text-anchor": "start" }, svg);
      lb.textContent = `${tv}%`;
    }
  }

  const drawSeries = (defs, dom) => {
    if (!dom) return;
    const y = scaleFor(dom);
    for (const s of defs) {
      if (!s.visible) continue;
      const d = C.seriesPath(spec.ts, s.values, x, y, spec.gapMs, s.step);
      if (!d) continue;
      svgEl("path", {
        d, fill: "none", stroke: `var(${s.color})`, "stroke-width": 2,
        "stroke-linejoin": "round", "stroke-linecap": "round",
        ...(s.dash ? { "stroke-dasharray": "5 4" } : {}),
      }, svg);
    }
  };
  drawSeries(spec.tempDefs, tDom);
  drawSeries(spec.humDefs, hDom);

  // Direct value labels on the setpoint lines. Setpoints are flat and dashed,
  // so color alone doesn't separate them — and because the y-scale rescales
  // when one is toggled off, the next line slides into the exact spot the
  // hidden one occupied. The value (with ° or % naming its axis) makes each
  // line self-identifying.
  const labels = [];
  const collect = (defs, dom, unit) => {
    if (!dom) return;
    const y = scaleFor(dom);
    for (const s of defs) {
      if (!s.visible || !s.step) continue;
      let last = null;
      for (let i = spec.ts.length - 1; i >= 0; i--)
        if (s.values[i] != null) { last = s.values[i]; break; }
      if (last != null) labels.push({ y: y(last), text: `${Math.round(last)}${unit}`, color: s.color });
    }
  };
  collect(spec.tempDefs, tDom, "°");
  collect(spec.humDefs, hDom, "%");
  labels.sort((a, b) => a.y - b.y);
  for (let i = 1; i < labels.length; i++)
    if (labels[i].y - labels[i - 1].y < 12) labels[i].y = labels[i - 1].y + 12;
  for (const L of labels) {
    const el = svgEl("text", { x: width - M.right - 5, y: L.y - 4, "text-anchor": "end" }, svg);
    el.style.fill = `var(${L.color})`;   // style beats the .lanes text CSS rule
    el.style.fontSize = "10px";
    el.style.fontWeight = "600";
    el.textContent = L.text;
  }

  // Equipment strips below the plot. Solid where it ran for the whole
  // reading; fainter where (at wide ranges) it ran for part of a bucket.
  const half = Math.min(spec.gapMs, C.medianInterval(spec.ts)) / 2;
  strips.forEach((s, r) => {
    const yTop = plotBottom + 6 + r * ROW;
    const label = svgEl("text", { x: M.left, y: yTop + 9, class: "eq-label" }, svg);
    label.textContent = s.label;
    svgEl("line", {
      x1: M.left, x2: width - M.right, y1: yTop + 21, y2: yTop + 21,
      stroke: "var(--grid)", "stroke-width": 1,
    }, svg);
    const byLevel = new Map();
    for (const run of C.stripRuns(spec.ts, s.values, spec.gapMs, half, t0, t1)) {
      const x0 = x(run.start), w = Math.max(x(run.end) - x0, 2);
      byLevel.set(run.level,
        (byLevel.get(run.level) || "") + `M${x0.toFixed(1)},${yTop + 12}h${w.toFixed(1)}v9h${(-w).toFixed(1)}z`);
    }
    for (const [level, d] of byLevel)
      svgEl("path", { d, fill: `var(${s.color})`, "fill-opacity": 0.3 + 0.7 * level }, svg);
  });

  drawXAxis(svg, x, t0, t1, width, H, M);
}

function drawXAxis(svg, x, t0, t1, width, H, M) {
  const span = t1 - t0;
  const count = Math.max(2, Math.floor((width - M.left - M.right) / 110));
  svgEl("line", {
    x1: M.left, x2: width - M.right, y1: H - 22, y2: H - 22,
    stroke: "var(--baseline)", "stroke-width": 1,
  }, svg);
  for (let i = 0; i <= count; i++) {
    const t = t0 + (span * i) / count;
    const label = svgEl("text", {
      x: x(t), y: H - 6,
      "text-anchor": i === 0 ? "start" : i === count ? "end" : "middle",
    }, svg);
    label.textContent = fmtTick(t, span);
  }
}

/* ---------- panel ---------- */

function legendChips(parent, ident, entries, onToggle) {
  for (const e of entries) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip";
    chip.dataset.key = e.storageKey;
    chip.setAttribute("aria-pressed", String(e.visible));
    const key = document.createElement("span");
    key.className = "key" + (e.dash ? " dashed" : "") + (e.block ? " block" : "");
    key.style.color = `var(${e.color})`;
    chip.appendChild(key);
    chip.appendChild(document.createTextNode(e.label));
    chip.addEventListener("click", () => {
      setVisible(ident, e.storageKey, chip.getAttribute("aria-pressed") !== "true");
      onToggle(e.storageKey);
    });
    parent.appendChild(chip);
  }
}

function gripIcon() {
  const svg = svgEl("svg", { viewBox: "0 0 20 20", "aria-hidden": "true", focusable: "false" });
  for (const cy of [4, 10, 16]) for (const cx of [7, 13]) svgEl("circle", { cx, cy, r: 1.7 }, svg);
  return svg;
}

function buildHeader(t) {
  const head = document.createElement("div");
  head.className = "panel-head";
  const name = t.display_name || t.name || "Thermostat";

  const grip = document.createElement("button");
  grip.type = "button";
  grip.className = "grip";
  grip.title = "Drag to reorder (or focus and use ↑ ↓)";
  grip.setAttribute("aria-label", `Move ${name}. Use the up and down arrow keys.`);
  grip.setAttribute("aria-describedby", "reorderHelp");
  grip.appendChild(gripIcon());
  head.appendChild(grip);

  const title = document.createElement("div");
  title.className = "panel-title";
  const h2 = document.createElement("h2");
  h2.textContent = name;
  if (t.name && t.name !== name) h2.title = `Called “${t.name}” in the ecobee app`;
  title.appendChild(h2);

  const L = t.latest;
  const running = L.equipment_status
    ? L.equipment_status.split(",").filter(Boolean).map((tok) => eqMeta(tok).label.toLowerCase()).join(", ")
    : "nothing";
  const climate = C.climateLabel(L.current_climate);
  const meta = document.createElement("div");
  meta.className = "meta";
  meta.textContent =
    `${C.fmtTemp(L.actual_temp_f)} · ${C.fmtHum(L.actual_humidity)} humidity · ` +
    `${C.modeLabel(L.hvac_mode)} mode${climate ? ` · ${climate}` : ""} · running: ${running} · ` +
    `as of ${fmtFull(L.ts)}`;
  title.appendChild(meta);

  const now = Date.now();
  const flags = [];
  if (C.isStale(L.ts, now)) flags.push(`No new readings since ${fmtFull(L.ts)} (${C.fmtAgo(now - L.ts)})`);
  if (!L.connected) flags.push("Thermostat is offline (not connected to ecobee)");
  for (const f of flags) {
    const flag = document.createElement("div");
    flag.className = "flag";
    flag.textContent = f;
    title.appendChild(flag);
  }
  head.appendChild(title);
  return { head, grip, name };
}

function buildPanel(t, domain, ref) {
  const root = panelsRoot();
  const panel = document.createElement("section");
  panel.className = "panel";
  panel.dataset.identifier = t.identifier;
  root.insertBefore(panel, ref || null); // attach before drawing so clientWidth is real

  const { head, grip, name } = buildHeader(t);
  panel.setAttribute("aria-label", name);
  panel.appendChild(head);
  attachReorder(panel, head, grip, name);

  if (!t.ts.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = `No readings in ${rangeText()}.`;
    panel.appendChild(empty);
    return panel;
  }

  const sensorSeries = t.sensors.map((s, i) => ({
    key: `sensor:${s.name}`, label: s.name, color: SENSOR_COLORS[i % SENSOR_COLORS.length],
    values: s.temperature_f,
  }));
  const tempDefs = TEMP_SERIES.map((d) => ({ ...d, values: t.series[d.key] })).concat(sensorSeries);
  const humDefs = HUM_SERIES.map((d) => ({ ...d, values: t.series[d.key] }));
  for (const d of tempDefs.concat(humDefs)) {
    d.storageKey = d.key;
    d.visible = isVisible(t.identifier, d.key);
  }
  const eqStrips = Object.keys(t.equipment).map((tok) => ({
    tok, ...eqMeta(tok), values: t.equipment[tok], block: true,
    storageKey: `eq:${tok}`, visible: isVisible(t.identifier, `eq:${tok}`),
  }));
  const ts = t.ts;
  const gapMs = C.medianInterval(ts) * 3;

  const lanes = document.createElement("div");
  lanes.className = "lanes";
  panel.appendChild(lanes);

  const onToggle = (key) => rerenderPanel(panel, t, key);
  const addKeyRow = (title, entries) => {
    const row = document.createElement("div");
    row.className = "lane-head";
    const titleEl = document.createElement("span");
    titleEl.className = "lane-title";
    titleEl.textContent = title;
    row.appendChild(titleEl);
    legendChips(row, t.identifier, entries, onToggle);
    lanes.appendChild(row);
  };
  addKeyRow("Temperature (°F, left scale)", tempDefs);
  addKeyRow("Relative humidity (%, right scale)", humDefs);
  if (eqStrips.length) addKeyRow("Equipment running", eqStrips);

  const chart = document.createElement("div");
  chart.className = "chart";
  lanes.appendChild(chart);
  const margin = marginsFor(chart.clientWidth);
  drawCombined(chart, {
    ts, tempDefs, humDefs, eqStrips, domain, gapMs, margin,
    label: `Chart of temperature, humidity and equipment for ${name} over ${rangeText()}. ` +
           `The data table below lists the same readings.`,
  });
  attachHover(chart, { ts, tempDefs, humDefs, eqStrips, domain, margin });
  panel.appendChild(buildTable(t));
  return panel;
}

/* Re-draw one panel (after a legend toggle) without touching the others,
   keeping keyboard focus on the chip that was pressed. */
function rerenderPanel(old, t, focusKey) {
  if (state.drag) { state.pendingRender = true; return; }
  const fresh = buildPanel(t, domainFor(state.data), old);
  old.remove();
  const chip = [...fresh.querySelectorAll(".chip")].find((c) => c.dataset.key === focusKey);
  if (chip) chip.focus({ preventScroll: true });
}

/* Crosshair + one tooltip per chart. Mouse: hover. Touch: tap, or swipe
   sideways (vertical swipes still scroll). Keyboard: focus, then ← →. */
function attachHover(chart, ctx) {
  const cross = document.createElement("div");
  cross.className = "crosshair";
  chart.appendChild(cross);
  const tip = document.createElement("div");
  tip.className = "tooltip";
  chart.appendChild(tip);
  chart.tabIndex = 0;
  let idx = -1;

  const plotW = () => chart.clientWidth - ctx.margin.left - ctx.margin.right || 1;
  const span = ctx.domain[1] - ctx.domain[0] || 1;
  const xToTime = (px) => ctx.domain[0] + ((px - ctx.margin.left) / plotW()) * span;
  const timeToX = (t) => ctx.margin.left + ((t - ctx.domain[0]) / span) * plotW();

  function nearestIndex(time) {
    const ts = ctx.ts;
    if (!ts.length) return -1;
    let lo = 0, hi = ts.length - 1;
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (ts[mid] < time) lo = mid; else hi = mid;
    }
    return time - ts[lo] <= ts[hi] - time ? lo : hi;
  }

  function ttRow(color, valText, lblText, dash) {
    const row = document.createElement("div");
    row.className = "tt-row";
    const key = document.createElement("span");
    key.className = "key" + (dash ? " dashed" : "");
    key.style.color = `var(${color})`;
    row.appendChild(key);
    const val = document.createElement("span");
    val.className = "val";
    val.textContent = valText;
    row.appendChild(val);
    const lbl = document.createElement("span");
    lbl.className = "lbl";
    lbl.textContent = lblText;
    row.appendChild(lbl);
    return row;
  }

  function show(i) {
    idx = i;
    if (i < 0 || i >= ctx.ts.length) { hide(); return; }
    const xPix = timeToX(ctx.ts[i]);
    cross.style.display = "block";
    cross.style.left = xPix + "px";
    cross.style.height = chart.clientHeight + "px";

    tip.textContent = "";
    const time = document.createElement("div");
    time.className = "tt-time";
    time.textContent = fmtFull(ctx.ts[i]);
    tip.appendChild(time);
    for (const d of ctx.tempDefs)
      if (d.visible && d.values[i] != null) tip.appendChild(ttRow(d.color, C.fmtTemp(d.values[i]), d.label, d.dash));
    for (const d of ctx.humDefs)
      if (d.visible && d.values[i] != null) tip.appendChild(ttRow(d.color, C.fmtHum(d.values[i]), d.label, d.dash));
    if (ctx.eqStrips.length) {
      const eq = document.createElement("div");
      eq.className = "tt-eq";
      const on = ctx.eqStrips.filter((s) => s.visible && s.values[i] > 0).map((s) => eqText(s.tok, s.values[i]));
      eq.textContent = on.length ? "Running: " + on.join(", ") : "Equipment idle";
      tip.appendChild(eq);
    }
    tip.style.display = "block";
    const w = chart.clientWidth;
    const tw = tip.offsetWidth;
    tip.style.left = Math.max(0, xPix + 12 + tw > w ? xPix - tw - 12 : xPix + 12) + "px";
    tip.style.top = "8px";
  }
  function hide() {
    idx = -1;
    cross.style.display = "none";
    tip.style.display = "none";
  }
  const hideEntry = { el: chart, hide };
  state.tips.add(hideEntry);

  const showAt = (ev) => {
    if (state.drag) return;
    const rect = chart.getBoundingClientRect();
    show(nearestIndex(xToTime(ev.clientX - rect.left)));
  };
  chart.addEventListener("pointerdown", (ev) => { if (ev.pointerType !== "mouse") showAt(ev); });
  chart.addEventListener("pointermove", showAt);
  // A finger lifting off shouldn't make the reading vanish before it's
  // read; a tap elsewhere (see hideTooltipsOutside) or a scroll hides it.
  chart.addEventListener("pointerleave", (ev) => { if (ev.pointerType === "mouse") hide(); });
  chart.addEventListener("pointercancel", hide);
  chart.addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowLeft" || ev.key === "ArrowRight") {
      ev.preventDefault();
      const next = idx < 0 ? ctx.ts.length - 1 : idx + (ev.key === "ArrowRight" ? 1 : -1);
      show(Math.max(0, Math.min(ctx.ts.length - 1, next)));
    } else if (ev.key === "Escape") hide();
  });
  chart.addEventListener("blur", hide);
}

function hideTooltips(except) {
  for (const entry of [...state.tips]) {
    if (!entry.el.isConnected) state.tips.delete(entry);
    else if (!except || !entry.el.contains(except)) entry.hide();
  }
}

/* Accessible table view of the same slice. */
function buildTable(t) {
  const details = document.createElement("details");
  details.className = "table-view";
  details.open = state.openTables.has(t.identifier);
  details.addEventListener("toggle", () => {
    if (details.open) state.openTables.add(t.identifier);
    else state.openTables.delete(t.identifier);
  });
  const summary = document.createElement("summary");
  summary.textContent = "Data table";
  details.appendChild(summary);
  const scroll = document.createElement("div");
  scroll.className = "table-scroll";
  const table = document.createElement("table");
  table.className = "data";
  const headRow = document.createElement("tr");
  for (const h of ["Time", "Temp", "RH", "Heat sp", "Cool sp", "Dehum sp", "Out temp", "Out RH", "Equipment"]) {
    const th = document.createElement("th");
    th.scope = "col";
    th.textContent = h;
    headRow.appendChild(th);
  }
  table.appendChild(headRow);

  const n = t.ts.length;
  const start = Math.max(0, n - 200);
  for (let i = n - 1; i >= start; i--) {
    const tr = document.createElement("tr");
    const cells = [
      fmtFull(t.ts[i]),
      C.fmtTemp(t.series.actual_temp_f[i]),
      C.fmtHum(t.series.actual_humidity[i]),
      C.fmtTemp(t.series.desired_heat_f[i]),
      C.fmtTemp(t.series.desired_cool_f[i]),
      C.fmtHum(t.series.desired_dehumidity[i]),
      C.fmtTemp(t.series.outdoor_temp_f[i]),
      C.fmtHum(t.series.outdoor_humidity[i]),
    ];
    for (const c of cells) {
      const td = document.createElement("td");
      td.textContent = c;
      tr.appendChild(td);
    }
    const eqTd = document.createElement("td");
    eqTd.className = "eq";
    const on = Object.entries(t.equipment)
      .filter(([, arr]) => arr[i] > 0)
      .map(([tok, arr]) => eqText(tok, arr[i]));
    eqTd.textContent = on.join(", ") || "idle";
    tr.appendChild(eqTd);
    table.appendChild(tr);
  }
  const notes = [];
  if (n > 200) notes.push(`Newest 200 of ${n.toLocaleString()} rows.`);
  if (state.data.bucket_s)
    notes.push(`Each row averages ${C.fmtDuration(state.data.bucket_s)} of readings; ` +
               `equipment percentages are the share of that time it ran.`);
  if (notes.length) {
    const caption = document.createElement("caption");
    caption.textContent = notes.join(" ");
    table.appendChild(caption);
  }
  scroll.appendChild(table);
  details.appendChild(scroll);
  return details;
}

/* ---------- reordering ---------- */
/* Drag the grip (finger, pen or mouse), or the title bar with a mouse, or
   focus the grip and press ↑/↓. While dragging, every panel collapses to its
   title bar, so even on a phone the whole list fits on screen. The order is
   saved in this browser only. */

function savePanelOrder() {
  const ids = [...panelsRoot().querySelectorAll(".panel")].map((p) => p.dataset.identifier);
  store.set(ORDER_KEY, JSON.stringify(ids));
}
function savedOrder() {
  try { return JSON.parse(store.get(ORDER_KEY)); } catch { return []; }
}
const panelIndex = (panel) => [...panelsRoot().children].indexOf(panel);

function attachReorder(panel, head, grip, name) {
  grip.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0 || state.drag) return;
    ev.preventDefault();   // no text selection, no long-press menu
    beginDrag(panel, grip, ev, ev.clientY, name);
  });
  // Stop the browser's native drag of the grip's contents.
  grip.addEventListener("dragstart", (ev) => ev.preventDefault());
  grip.addEventListener("contextmenu", (ev) => ev.preventDefault());

  // Mouse users can also grab the title bar; a few pixels of movement are
  // required first so ordinary clicks and text selection still work.
  head.classList.add("mouse-drag");
  head.addEventListener("pointerdown", (ev) => {
    if (ev.pointerType !== "mouse" || ev.button !== 0 || state.drag || grip.contains(ev.target)) return;
    const x0 = ev.clientX, y0 = ev.clientY;
    const onMove = (e) => {
      if (Math.hypot(e.clientX - x0, e.clientY - y0) < 5) return;
      stop();
      beginDrag(panel, head, e, y0, name);
    };
    const stop = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", stop);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", stop);
  });

  grip.addEventListener("keydown", (ev) => {
    const delta = { ArrowUp: -1, ArrowDown: 1 }[ev.key];
    if (!delta) return;
    ev.preventDefault();
    movePanelBy(panel, delta, grip, name);
  });
}

function beginDrag(panel, handle, ev, grabY, name) {
  const root = panelsRoot();
  hideTooltips();
  const before = panel.getBoundingClientRect();
  const drag = {
    panel, handle, name, pointerId: ev.pointerId, y: ev.clientY,
    grabOffset: grabY - before.top, startIndex: panelIndex(panel), raf: 0,
  };
  state.drag = drag;
  try { handle.setPointerCapture(ev.pointerId); } catch { /* window listeners still see it */ }

  document.body.classList.add("reorder-active");
  root.classList.add("reordering");
  panel.classList.add("lifted");
  // Collapsing the panels moved everything; scroll so the grabbed title bar
  // stays under the finger instead of jumping away from it.
  window.scrollBy(0, panel.getBoundingClientRect().top - before.top);
  drag.grabOffset = Math.min(drag.grabOffset, panel.offsetHeight - 8);
  followPointer();

  drag.onMove = (e) => {
    if (e.pointerId !== drag.pointerId) return;
    drag.y = e.clientY;
    followPointer();
  };
  drag.onEnd = (e) => { if (!e || e.pointerId === drag.pointerId) endDrag(); };
  window.addEventListener("pointermove", drag.onMove);
  window.addEventListener("pointerup", drag.onEnd);
  window.addEventListener("pointercancel", drag.onEnd);
  drag.raf = requestAnimationFrame(autoScroll);
}

/* Put the dragged panel in the slot its center is over, animate the panels
   it displaced, and keep it drawn under the pointer. */
function followPointer() {
  const d = state.drag;
  if (!d) return;
  const root = panelsRoot();
  const panel = d.panel;
  const rootTop = root.getBoundingClientRect().top;
  const top = d.y - d.grabOffset - rootTop;             // where it's drawn, in #panels coordinates
  const others = [...root.children].filter((p) => p !== panel);
  // offsetTop ignores transforms, so animations in flight don't skew this.
  const mids = others.map((p) => p.offsetTop + p.offsetHeight / 2);
  const ref = others[C.dropIndex(top + panel.offsetHeight / 2, mids)] || null;
  if (panel.nextElementSibling !== ref) {
    const was = new Map(others.map((p) => [p, p.offsetTop]));
    root.insertBefore(panel, ref);
    // Moving the node can release pointer capture; take it back so the
    // drag keeps receiving events.
    try { d.handle.setPointerCapture(d.pointerId); } catch { /* pointer already up */ }
    for (const p of others) {
      const dy = was.get(p) - p.offsetTop;
      if (dy) slide(p, dy);
    }
  }
  panel.style.transform = `translateY(${top - panel.offsetTop}px)`;
}

function slide(p, dy) {
  p.classList.remove("settling");
  p.style.transform = `translateY(${dy}px)`;
  void p.offsetWidth;                  // commit the start position
  p.classList.add("settling");
  p.style.transform = "";
}

/* Scroll while the pointer rests near the top or bottom edge. */
function autoScroll() {
  const d = state.drag;
  if (!d) return;
  const v = C.autoScrollSpeed(d.y, window.innerHeight);
  if (v) {
    window.scrollBy(0, v);
    followPointer();
  }
  d.raf = requestAnimationFrame(autoScroll);
}

function endDrag() {
  const d = state.drag;
  if (!d) return;
  state.drag = null;
  cancelAnimationFrame(d.raf);
  window.removeEventListener("pointermove", d.onMove);
  window.removeEventListener("pointerup", d.onEnd);
  window.removeEventListener("pointercancel", d.onEnd);
  try { d.handle.releasePointerCapture(d.pointerId); } catch { /* already released */ }

  const root = panelsRoot();
  for (const p of root.children) { p.classList.remove("settling"); p.style.transform = ""; }
  // Expand again, keeping the dropped panel's title bar where it landed.
  const before = d.panel.getBoundingClientRect().top;
  root.classList.remove("reordering");
  d.panel.classList.remove("lifted");
  window.scrollBy(0, d.panel.getBoundingClientRect().top - before);
  document.body.classList.remove("reorder-active");

  savePanelOrder();
  const pos = panelIndex(d.panel);
  if (pos !== d.startIndex) announce(`${d.name} moved to position ${pos + 1} of ${root.children.length}.`);
  if (state.pendingRender) { state.pendingRender = false; renderPanels(); }
}

function movePanelBy(panel, delta, focusEl, name) {
  const root = panelsRoot();
  const panels = [...root.children];
  const from = panels.indexOf(panel);
  const order = C.moveBy(panels.map((p) => p.dataset.identifier), from, delta);
  const to = order.indexOf(panel.dataset.identifier);
  if (to === from) return;
  const byId = new Map(panels.map((p) => [p.dataset.identifier, p]));
  for (const id of order) root.appendChild(byId.get(id));
  focusEl.focus({ preventScroll: true });
  panel.scrollIntoView({ block: "nearest" });
  savePanelOrder();
  announce(`${name} moved to position ${to + 1} of ${panels.length}.`);
}

/* ---------- top level ---------- */

function rangeText() {
  return (RANGES.find((r) => r.hours === state.hours) || RANGES[1]).text;
}

function domainFor(data) {
  const now = Date.now();
  if (state.hours > 0) return [now - state.hours * 3600e3, now];
  let lo = Infinity, hi = -Infinity;
  for (const t of data.thermostats) {
    if (t.ts.length) { lo = Math.min(lo, t.ts[0]); hi = Math.max(hi, t.ts[t.ts.length - 1]); }
  }
  if (lo === Infinity) { lo = now - 3600e3; hi = now; }
  return [lo, Math.max(hi, lo + 60e3)];
}

function renderPanels() {
  if (state.drag) { state.pendingRender = true; return; }
  const root = panelsRoot();
  const focusedKey = document.activeElement && document.activeElement.dataset
    ? { panel: document.activeElement.closest(".panel")?.dataset.identifier,
        chip: document.activeElement.dataset.key,
        grip: document.activeElement.classList.contains("grip") }
    : null;
  state.tips.clear();
  root.textContent = "";
  state.width = root.clientWidth;
  const data = state.data;
  if (!data || !data.thermostats.length) {
    const div = document.createElement("div");
    div.className = "empty";
    div.textContent = "No thermostats yet. Readings appear here a few minutes after the logger starts.";
    root.appendChild(div);
    return;
  }
  const domain = domainFor(data);
  for (const t of C.applySavedOrder(data.thermostats, savedOrder())) buildPanel(t, domain, null);

  // Keep keyboard focus across the periodic refresh.
  if (focusedKey && focusedKey.panel) {
    const panel = [...root.children].find((p) => p.dataset.identifier === focusedKey.panel);
    const target = panel && (focusedKey.grip ? $(".grip", panel)
      : [...panel.querySelectorAll(".chip")].find((c) => c.dataset.key === focusedKey.chip));
    if (target) target.focus({ preventScroll: true });
  }
}

function setStatus(text) { $("#status").textContent = text; }

async function fetchData() {
  if (state.controller) state.controller.abort();   // newest request wins
  const controller = new AbortController();
  state.controller = controller;
  clearTimeout(state.refreshTimer);
  document.querySelectorAll(".panel").forEach((p) => p.classList.add("loading"));
  setStatus("loading…");
  try {
    // relative URL so the page works both at / (standalone) and under a subpath
    const resp = await fetch(`api/data?hours=${state.hours}`, { signal: controller.signal });
    if (!resp.ok) throw new Error(`the server answered HTTP ${resp.status}`);
    const data = await resp.json();
    if (!data || !Array.isArray(data.thermostats)) throw new Error("the server sent unexpected data");
    state.data = data;
    state.fetchedAt = Date.now();
    let status = `${C.plural(data.thermostats.length, "thermostat")} · ${C.plural(data.readings || 0, "reading")}`;
    if (data.bucket_s) status += ` · averaged per ${C.fmtDuration(data.bucket_s)}`;
    setStatus(`${status} · updated ${fmtClock(state.fetchedAt)}`);
    renderPanels();
  } catch (err) {
    if (err.name === "AbortError") return;
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("loading"));
    setStatus(state.data
      ? `Couldn't refresh (${err.message}); showing data from ${fmtClock(state.fetchedAt)}.`
      : `Couldn't load data (${err.message}). Try Refresh in a minute.`);
  } finally {
    if (state.controller === controller) {
      state.controller = null;
      scheduleRefresh(REFRESH_MS);
    }
  }
}

/* Refresh every few minutes, but not in a hidden tab; catch up on return. */
function scheduleRefresh(delay) {
  clearTimeout(state.refreshTimer);
  state.refreshTimer = setTimeout(() => { if (!document.hidden) fetchData(); }, delay);
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden || state.controller) return;
  const age = Date.now() - state.fetchedAt;
  if (age >= REFRESH_MS) fetchData();
  else scheduleRefresh(REFRESH_MS - age);
});

function buildRangeSeg() {
  const seg = $("#rangeSeg");
  for (const r of RANGES) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = r.label;
    b.setAttribute("aria-label", `Show ${r.text}`);
    b.setAttribute("aria-pressed", String(r.hours === state.hours));
    b.addEventListener("click", () => {
      if (state.hours === r.hours && state.data) return;
      state.hours = r.hours;
      seg.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", "false"));
      b.setAttribute("aria-pressed", "true");
      fetchData();
    });
    seg.appendChild(b);
  }
}

buildRangeSeg();
$("#refreshBtn").addEventListener("click", fetchData);
document.addEventListener("pointerdown", (ev) => hideTooltips(ev.target), true);
fetchData();

// Phones fire resize when the address bar slides in or out while scrolling.
// Only a width change affects the charts, so ignore the rest.
let resizeT = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeT);
  resizeT = setTimeout(() => {
    if (panelsRoot().clientWidth !== state.width) renderPanels();
  }, 150);
});
})();
