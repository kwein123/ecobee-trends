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
   mid-run; ecobee rotates refresh tokens, and rotations are persisted back.
   Transient API failures are logged and retried on the next tick rather than
   crashing the loop.
3. **Store** (SQLite, WAL mode). WAL lets the dashboard read while the logger
   writes without either blocking. Temperatures arrive from ecobee in tenths
   of °F and are normalized to °F on write; ecobee's "unknown" sentinels
   (`-5002`, `-5003`) become `NULL` rather than absurd readings.
4. **Serve** (`app/ecobee_dashboard.py`). Standard-library HTTP server —
   no framework. Opens the database read-only (`mode=ro`), reshapes rows into
   columnar JSON, and caches responses for 60 s (the data only changes every
   5 minutes, so this bounds the cost of repeat requests).
5. **Render** (`app/ecobee_dashboard.html`). Hand-built SVG — no chart library,
   no CDN, no build step. The page is self-contained and works offline.

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
- **Downsampling** above ~1200 points: numeric series are averaged per bucket,
  equipment states are OR-ed (a bucket is "on" if the equipment ran at all in
  it), so brief cycles never disappear at wide zoom.
- **Gaps stay gaps.** If the logger was down, the line breaks rather than
  interpolating across missing hours.

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
