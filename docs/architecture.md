# Architecture

## Shape

Two containers built from one image, sharing one directory on the host:

| Service | Job | Mounts |
|---|---|---|
| `logger` | Polls the ecobee API every `POLL_INTERVAL` seconds, appends readings | `data/auth/` (tokens), `data/db/` |
| `dashboard` | Serves the page + JSON API on :8321 | `data/db/` **only** |

Splitting them isn't ceremony — it's what makes the credential isolation in
[security.md](security.md) real, and it means the dashboard can be restarted,
exposed, or replaced without touching the thing that holds tokens.

## Data flow

1. **Auth** (`app/ecobee_login.py`, once). Performs ecobee's web OAuth flow,
   including two-factor challenge, and writes `access_token` + `refresh_token`
   to `data/auth/ecobee.conf` with mode 600. The password is scrubbed before
   the file is written — the refresh token is all that's needed afterward.
2. **Poll** (`app/ecobee_logger.py`, forever). One API call returns every
   thermostat on the account. Expired access tokens refresh automatically
   mid-run; ecobee rotates refresh tokens, and rotations are persisted back
   with an atomic write (temp file, fsync, rename), because that file is the
   only copy. Transient API failures are logged and retried on the next tick
   rather than crashing the loop. Each poll is one transaction: a failure
   halfway rolls back instead of leaving a partial poll behind. If the tokens
   are revoked, the logger waits for a new token file (re-run the login)
   rather than exiting, which under Docker's restart policy would retry a
   dead token against ecobee's auth server forever.
3. **Store** (SQLite, WAL mode). WAL lets the dashboard read while the logger
   writes without either blocking. Temperatures arrive from ecobee in tenths
   of °F and are normalized to °F on write; ecobee's "unknown" sentinels
   (`-5002`, `-5003`) become `NULL` rather than absurd readings.
4. **Serve** (`app/ecobee_dashboard.py`). Standard-library HTTP server,
   no framework. Opens the database read-only (`mode=ro`) and reshapes rows
   into columnar JSON. Queries run per thermostat so every one is an index
   range seek on `(identifier, ts_utc)`, never a table scan. Ranges longer
   than 48 hours are grouped in SQL into round buckets (15 min, 1 h, …)
   so no response carries more than 800 points per thermostat: a year of
   history is ~130 KB instead of ~20 MB. Responses are gzipped and held in a
   small LRU cache (8 entries, 60 s), so repeat requests are nearly free and
   varying the query can't grow memory.
5. **Render** (`app/ecobee_dashboard.html` + `app/static/`). Hand-built SVG,
   no chart library, no CDN, no build step. `dashboard-core.js` holds the
   pure logic (scales, paths, ordering) and is unit-tested under Node;
   `dashboard.js` does the DOM work. No inline script or style, so the
   page can be served with a strict Content-Security-Policy.

## Schema

`app/schema.py` holds the table definitions, imported by both the logger and
the demo generator so they can never drift apart.

- **`thermostat_readings`** — one row per thermostat per poll: mode, climate,
  `equipment_status` (comma-separated list of what's running), indoor
  temp/humidity, heat/cool setpoints, humidify/dehumidify targets, fan
  settings, active event (hold/vacation), outdoor temp/humidity/condition.
- **`sensor_readings`** — one row per remote sensor per poll: temperature,
  humidity, occupancy.
- **`unit_prefs`** — `identifier` → `display_name`, `sort_order`. Presentation
  only: logged rows keep the names ecobee reports, so renaming never rewrites
  history and is reversible.
- **`meta`** — small key/value table; currently holds `public_id_salt`.

## Charting decisions

- **One chart per thermostat, not three.** Temperature (left °F axis),
  humidity (right % axis) and equipment run-state share one x-axis, because
  the interesting questions are correlations: did the dehumidifier actually
  pull humidity down, does the compressor short-cycle at dusk.
- **Two y-axes, used carefully.** Dual axes are usually a data-viz anti-pattern
  because they let you imply correlations by scaling. Here the two measures
  are in fixed, physically meaningful units that users already reason about
  together on a thermostat, each series is labeled with its unit, and
  gridlines follow one axis only.
- **Setpoints carry direct value labels** (`72°`, `55%`). They're flat dashed
  lines, and when you toggle one off the axis rescales — without labels the
  next line slides into the vacated position and looks like nothing happened.
- **Colorblind-safe palette**, validated for adjacent-pair separation, with
  dark and light variants. Series identity never rests on color alone: every
  series has a legend key, and dashed lines are drawn dashed in the key too.
- **Averaging at wide ranges, done by the server.** Measurements are
  averaged per bucket; setpoints take the value in force at the bucket's end
  (they're step functions, and an average would invent values nobody set);
  equipment becomes the *share* of readings it was on, drawn as a paler bar
  the smaller the share. Shares are rounded *up* to quarters, so a brief
  cycle still leaves a visible mark at year scale.
- **Gaps stay gaps.** If the logger was down, the line breaks rather than
  interpolating across missing hours.
- **Every known thermostat gets a panel**, even with no readings in the
  selected range, with a warning once its last reading is over 30 minutes
  old. A unit that goes offline shows up as stale instead of silently
  vanishing.

## Reordering panels

Panels are reordered with **Pointer Events** on a dedicated handle, not the
HTML drag-and-drop API. Drag-and-drop is a desktop-mouse API: on Android a
finger on the title bar just scrolled the page (the original bug, reproduced
with real touch input before the fix). The handle has `touch-action: none`,
so a finger there always drags and never scrolls; everywhere else, scrolling
is untouched.

While dragging, every panel collapses to its title bar (each is otherwise
taller than a phone screen), the page scrolls so the grabbed title stays
under the finger, and the page auto-scrolls near the screen edges. Mouse
users can also drag the title bar; keyboard users focus the handle and use
↑/↓, with the move announced to screen readers. Periodic refreshes and
resizes wait until the drop. The order is saved per browser in
`localStorage` (falling back to memory if storage is blocked).
`tests/browser/touch.test.mjs` verifies all of this in headless Chrome with
genuine touch events.

## Deploying behind a reverse proxy

The page fetches its API with **relative** URLs, so it works unmodified under
a subpath. With Caddy:

```caddy
redir /ecobee /ecobee/ 301
handle_path /ecobee/* {
    reverse_proxy ecobee-dashboard:8321
}
```

Put the dashboard container on the same Docker network as the proxy and drop
its host `ports:` mapping if you don't want direct access. Read
[security.md](security.md) first if the proxy is internet-facing.
