// End-to-end touch tests in a real, headless Chrome emulating a phone.
//
// Reordering once failed on Android because it relied on the HTML
// drag-and-drop API, which touch screens don't reliably support. Mouse-driven
// tests can't catch that, so these send genuine touch events through the
// Chrome DevTools Protocol (the same input path a finger takes).
//
//   node --test tests/browser/
//
// Needs Chrome/Chromium (set CHROME_PATH if it isn't found) and Python.
// Skips when no browser is available unless REQUIRE_BROWSER=1 (as in CI).

import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { spawn, execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const PYTHON = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function findChrome() {
  const candidates = [
    process.env.CHROME_PATH,
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
  ].filter(Boolean);
  for (const c of candidates) {
    if (c.includes("/")) { if (existsSync(c)) return c; continue; }
    try { return execFileSync("which", [c], { encoding: "utf8" }).trim(); } catch { /* next */ }
  }
  return null;
}

const CHROME = findChrome();
const skip = !CHROME && process.env.REQUIRE_BROWSER !== "1" ? "no Chrome/Chromium found" : false;

let tmp, server, chrome, cdp, browserWs, baseUrl;

/* Minimal CDP client over Node's built-in WebSocket. */
async function connect(wsUrl) {
  const ws = new WebSocket(wsUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let id = 0;
  const pending = new Map();
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result);
    }
  };
  return {
    send(method, params = {}) {
      return new Promise((resolve, reject) => {
        pending.set(++id, { resolve, reject });
        ws.send(JSON.stringify({ id, method, params }));
      });
    },
    close() { ws.close(); },
  };
}

async function evaluate(expr) {
  const r = await cdp.send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || "evaluate failed");
  return r.result.value;
}

async function waitFor(expr, timeoutMs = 10000) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    if (await evaluate(expr).catch(() => false)) return;
    await sleep(50);
  }
  throw new Error(`timed out waiting for: ${expr}`);
}

const touch = (type, x, y) => cdp.send("Input.dispatchTouchEvent", {
  type, touchPoints: type === "touchEnd" ? [] : [{ x, y, id: 1, radiusX: 4, radiusY: 4, force: 1 }],
});

/* Press at (x, y), glide to (x, toY) in small steps like a finger, hold, lift. */
async function touchDrag(x, y, toY, { step = 14, holdMs = 250 } = {}) {
  await touch("touchStart", x, y);
  await sleep(30);
  const dir = Math.sign(toY - y);
  for (let cur = y; dir * (toY - cur) > 0; cur += dir * step) {
    await touch("touchMove", x, cur);
    await sleep(16);
  }
  await touch("touchMove", x, toY);
  await sleep(holdMs);
  await touch("touchEnd");
  await sleep(100);
}

const order = () => evaluate(`[...document.querySelectorAll('#panels .panel h2')].map(h => h.textContent)`);

async function loadFresh() {
  await evaluate("localStorage.clear()");
  await cdp.send("Page.navigate", { url: baseUrl });
  await waitFor("document.querySelectorAll('#panels .panel .chart svg').length === 3");
}

before(async () => {
  if (skip) return;
  assert.ok(CHROME, "REQUIRE_BROWSER=1 but no Chrome/Chromium found; set CHROME_PATH");
  tmp = mkdtempSync(join(tmpdir(), "ecobee-touch-"));
  const db = join(tmp, "demo.sqlite3");
  execFileSync(PYTHON, [join(ROOT, "tools", "make_demo_db.py"), "--db", db, "--hours", "24"]);

  server = spawn(PYTHON, ["-u", join(ROOT, "app", "ecobee_dashboard.py"), "--db", db, "--port", "0", "--no-open"]);
  baseUrl = await new Promise((resolve, reject) => {
    let out = "";
    server.stdout.on("data", (d) => {
      out += d;
      const m = out.match(/(http:\/\/127\.0\.0\.1:\d+\/)/);
      if (m) resolve(m[1]);
    });
    server.on("exit", (code) => reject(new Error(`dashboard exited (${code}): ${out}`)));
  });

  const profile = join(tmp, "profile");
  chrome = spawn(CHROME, [
    "--headless=new", "--remote-debugging-port=0", `--user-data-dir=${profile}`,
    "--no-first-run", "--no-default-browser-check", "--disable-gpu", "--no-sandbox",
    "about:blank",
  ], { stdio: "ignore" });
  const portFile = join(profile, "DevToolsActivePort");
  for (let i = 0; i < 200 && !existsSync(portFile); i++) await sleep(50);
  const [port, browserPath] = readFileSync(portFile, "utf8").split("\n");
  browserWs = `ws://127.0.0.1:${port}${browserPath}`;
  let pages = [];
  for (let i = 0; i < 100 && !pages.length; i++) {
    pages = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).filter((t) => t.type === "page");
    if (!pages.length) await sleep(50);
  }
  cdp = await connect(pages[0].webSocketDebuggerUrl);
  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");
  // A mid-size Android phone: touch-only, mobile viewport.
  await cdp.send("Emulation.setDeviceMetricsOverride", { width: 393, height: 851, deviceScaleFactor: 2.75, mobile: true });
  await cdp.send("Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 5 });
  await cdp.send("Emulation.setUserAgentOverride", {
    userAgent: "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Mobile Safari/537.36",
  });
  await cdp.send("Page.navigate", { url: baseUrl });
  await waitFor("document.readyState === 'complete'");
});

after(async () => {
  cdp?.close();
  const exited = (p) => (p && p.exitCode === null ? new Promise((r) => p.once("exit", r)) : null);
  const waits = [exited(chrome), exited(server)];
  // Ask Chrome to quit: unlike kill(), that also stops its helper processes,
  // which otherwise keep writing to the profile while it's being deleted.
  try {
    const browser = await connect(browserWs);
    await Promise.race([browser.send("Browser.close").catch(() => {}), sleep(3000)]);
    browser.close();
  } catch { /* fall back to kill below */ }
  const killTimer = setTimeout(() => chrome?.kill("SIGKILL"), 5000);
  server?.kill();
  await Promise.all(waits);
  clearTimeout(killTimer);
  // Leftover temp files are not a test failure.
  try {
    if (tmp) rmSync(tmp, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 });
  } catch (err) {
    console.warn(`warning: could not remove ${tmp}: ${err.message}`);
  }
});

test("a finger on the grip drags the bottom panel to the top", { skip }, async () => {
  await loadFresh();
  assert.deepEqual(await order(), ["Lake House", "Main Floor", "Upstairs"]);
  assert.equal(await evaluate("matchMedia('(pointer: coarse)').matches"), true, "not emulating a touch screen");

  const grip = await evaluate(`(() => {
    const g = document.querySelectorAll('#panels .panel')[2].querySelector('.grip');
    g.scrollIntoView({ block: 'center' });
    const r = g.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width };
  })()`);
  assert.ok(grip.w >= 44, `grip is ${grip.w}px wide; fingers need at least 44px`);
  const scrollBefore = await evaluate("scrollY");

  await touch("touchStart", grip.x, grip.y);
  await sleep(30);
  await touch("touchMove", grip.x, grip.y - 4);
  await sleep(50);
  const mid = await evaluate(`({
    collapsed: document.querySelector('#panels').classList.contains('reordering'),
    fits: document.querySelector('#panels').offsetHeight < innerHeight,
    gripY: document.querySelector('.panel.lifted .grip').getBoundingClientRect().top,
  })`);
  assert.ok(mid.collapsed, "panels should collapse while dragging");
  assert.ok(mid.fits, "collapsed panels should all fit on one phone screen");
  assert.ok(Math.abs(mid.gripY + 22 - grip.y) < 30, `grip jumped away from the finger (${mid.gripY} vs ${grip.y})`);
  await touchDrag(grip.x, grip.y - 4, 30);

  assert.deepEqual(await order(), ["Upstairs", "Lake House", "Main Floor"]);
  const after = await evaluate(`({
    collapsed: document.querySelector('#panels').classList.contains('reordering'),
    transforms: [...document.querySelectorAll('#panels .panel')].map(p => p.style.transform).join(''),
    tall: [...document.querySelectorAll('#panels .panel')].every(p => p.offsetHeight > 300),
    saved: JSON.parse(localStorage.getItem('ecobee-viz:panel-order')).length,
  })`);
  assert.deepEqual(after, { collapsed: false, transforms: "", tall: true, saved: 3 });
  assert.ok(scrollBefore > 0, "test setup: page should have been scrolled to the third panel");
});

test("the new order survives a reload", { skip }, async () => {
  await cdp.send("Page.reload");
  await waitFor("document.querySelectorAll('#panels .panel .chart svg').length === 3");
  assert.deepEqual(await order(), ["Upstairs", "Lake House", "Main Floor"]);
});

test("a finger drags the top panel to the bottom, past the screen edge", { skip }, async () => {
  await loadFresh();
  await evaluate("scrollTo(0, 0)");
  const grip = await evaluate(`(() => {
    const r = document.querySelector('#panels .panel .grip').getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  })()`);
  // Drag into the bottom edge zone and hold there so auto-scroll runs.
  await touchDrag(grip.x, grip.y, 845, { holdMs: 600 });
  assert.deepEqual(await order(), ["Main Floor", "Upstairs", "Lake House"]);
});

test("a vertical swipe on a chart still scrolls the page", { skip }, async () => {
  await loadFresh();
  await evaluate("scrollTo(0, 0)");
  const chart = await evaluate(`(() => {
    const r = document.querySelector('#panels .panel .chart').getBoundingClientRect();
    return { x: r.left + r.width / 2, y: Math.min(r.top + 120, innerHeight - 200) };
  })()`);
  await touchDrag(chart.x, chart.y + 150, chart.y - 150, { step: 20, holdMs: 50 });
  await sleep(400);
  assert.ok(await evaluate("scrollY") > 100, "swiping up over a chart should scroll the page");
  assert.deepEqual(await order(), ["Lake House", "Main Floor", "Upstairs"], "scrolling must not reorder");
});

test("tapping a chart shows the reading under the finger", { skip }, async () => {
  await loadFresh();
  const pt = await evaluate(`(() => {
    const c = document.querySelector('#panels .panel .chart');
    c.scrollIntoView({ block: 'center' });
    const r = c.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + 100 };
  })()`);
  await touch("touchStart", pt.x, pt.y);
  await sleep(50);
  await touch("touchEnd");
  await sleep(100);
  const tip = await evaluate(`(() => {
    const t = document.querySelector('#panels .panel .tooltip');
    return { shown: t.style.display === 'block', text: t.textContent };
  })()`);
  assert.ok(tip.shown, "tooltip should stay visible after the finger lifts");
  assert.match(tip.text, /Indoor temp/);
});
