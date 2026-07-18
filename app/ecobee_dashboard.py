"""Local web dashboard for data recorded by ecobee_logger.py.

Serves a single page showing, per thermostat, aligned timelines of
temperature, humidity, and equipment run-state (fan, dehumidifier,
cooling, ...) pulled live from the SQLite database. Every series can be
toggled on/off. Uses only the Python standard library.

Usage:
    python app/ecobee_dashboard.py            # serves and opens the page
    python app/ecobee_dashboard.py --port 8321 --db ./data/db/ecobee.sqlite3
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DEFAULT_DB = os.environ.get("ECOBEE_DB", "./data/db/ecobee.sqlite3")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(SCRIPT_DIR, "ecobee_dashboard.html")
ASSETS_DIR = os.path.join(SCRIPT_DIR, "static")
ASSET_TYPES = {".css": "text/css", ".svg": "image/svg+xml", ".png": "image/png",
               ".ico": "image/x-icon", ".js": "text/javascript"}

READING_COLUMNS = [
    "actual_temp_f",
    "desired_heat_f",
    "desired_cool_f",
    "outdoor_temp_f",
    "actual_humidity",
    "desired_humidity",
    "desired_dehumidity",
    "outdoor_humidity",
]

# Display order for equipment strips: cooling, heating, air movement, moisture.
EQUIPMENT_ORDER = [
    "compCool1", "compCool2",
    "heatPump", "heatPump2", "heatPump3",
    "auxHeat1", "auxHeat2", "auxHeat3",
    "fan",
    "humidifier", "dehumidifier",
    "ventilator", "economizer",
    "compHotWater", "auxHotWater",
]


def _ts_ms(ts_utc: str) -> int:
    return int(
        datetime.strptime(ts_utc, "%Y-%m-%dT%H:%M:%SZ")
        .replace(tzinfo=timezone.utc)
        .timestamp()
        * 1000
    )


def build_payload(db_path: str, hours: int) -> dict:
    cutoff = None
    if hours > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 5000")
    try:
        where = "WHERE ts_utc >= ?" if cutoff else ""
        args = (cutoff,) if cutoff else ()
        rows = db.execute(
            f"""SELECT ts_utc, identifier, name, connected, hvac_mode,
                       current_climate, equipment_status, {", ".join(READING_COLUMNS)}
                FROM thermostat_readings {where}
                ORDER BY identifier, ts_utc""",
            args,
        ).fetchall()
        sensor_where = (
            "WHERE ts_utc >= ? AND sensor_type != 'thermostat'"
            if cutoff
            else "WHERE sensor_type != 'thermostat'"
        )
        sensor_rows = db.execute(
            f"""SELECT ts_utc, thermostat_identifier, sensor_name, temperature_f
                FROM sensor_readings {sensor_where}
                ORDER BY thermostat_identifier, ts_utc""",
            args,
        ).fetchall()
        try:
            prefs = {
                r["identifier"]: r
                for r in db.execute(
                    "SELECT identifier, display_name, sort_order FROM unit_prefs"
                )
            }
        except sqlite3.OperationalError:  # table not created yet
            prefs = {}
        try:
            row = db.execute(
                "SELECT value FROM meta WHERE key = 'public_id_salt'"
            ).fetchone()
            salt = row["value"] if row else ""
        except sqlite3.OperationalError:  # meta table not created yet
            salt = ""
    finally:
        db.close()

    thermostats: dict[str, dict] = {}
    for row in rows:
        t = thermostats.setdefault(
            row["identifier"],
            {
                "identifier": row["identifier"],
                "name": row["name"],
                "ts": [],
                "series": {c: [] for c in READING_COLUMNS},
                "equipment": {},
                "sensors": [],
                "latest": {},
            },
        )
        t["name"] = row["name"] or t["name"]
        t["ts"].append(_ts_ms(row["ts_utc"]))
        for col in READING_COLUMNS:
            t["series"][col].append(row[col])
        tokens = [tok for tok in (row["equipment_status"] or "").split(",") if tok]
        idx = len(t["ts"]) - 1
        for tok in tokens:
            t["equipment"].setdefault(tok, [])
        for tok, arr in t["equipment"].items():
            arr.extend([0] * (idx + 1 - len(arr)))
            arr[idx] = 1 if tok in tokens else 0
        t["latest"] = {
            "ts": t["ts"][-1],
            "connected": bool(row["connected"]),
            "hvac_mode": row["hvac_mode"],
            "current_climate": row["current_climate"],
            "equipment_status": row["equipment_status"] or "",
            "actual_temp_f": row["actual_temp_f"],
            "actual_humidity": row["actual_humidity"],
        }

    # Remote (non-built-in) sensor temperatures, mapped onto each poll timestamp.
    by_sensor: dict[tuple[str, str], dict[int, float]] = {}
    for row in sensor_rows:
        key = (row["thermostat_identifier"], row["sensor_name"])
        by_sensor.setdefault(key, {})[_ts_ms(row["ts_utc"])] = row["temperature_f"]
    for (ident, sensor_name), values in sorted(by_sensor.items()):
        t = thermostats.get(ident)
        if t is None:
            continue
        t["sensors"].append(
            {
                "name": sensor_name,
                "temperature_f": [values.get(ts) for ts in t["ts"]],
            }
        )

    for t in thermostats.values():
        # Pad equipment arrays to full length and order the strips sensibly.
        n = len(t["ts"])
        ordered = {}
        known = [tok for tok in EQUIPMENT_ORDER if tok in t["equipment"]]
        extra = sorted(tok for tok in t["equipment"] if tok not in EQUIPMENT_ORDER)
        for tok in known + extra:
            arr = t["equipment"][tok]
            arr.extend([0] * (n - len(arr)))
            ordered[tok] = arr
        t["equipment"] = ordered

    for t in thermostats.values():
        p = prefs.get(t["identifier"])
        t["display_name"] = (p["display_name"] if p else None) or t["name"]
        t["_sort"] = p["sort_order"] if p and p["sort_order"] is not None else None

    ordered = sorted(
        thermostats.values(),
        key=lambda t: (t["_sort"] is None, t["_sort"] or 0, (t["display_name"] or "").lower()),
    )
    for t in ordered:
        del t["_sort"]
        # Replace the real thermostat identifier (its serial number) with a
        # salted hash: stable for client-side keying (drag order, toggle
        # prefs), but the serial itself never leaves the server.
        t["identifier"] = hashlib.sha256(
            (salt + t["identifier"]).encode("utf-8")
        ).hexdigest()[:12]

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hours": hours,
        "thermostats": ordered,
    }


# Tiny response cache: the logger only writes every 5 minutes, so identical
# /api/data responses within 60s are free — this bounds the cost of anyone
# hammering the public endpoint (belt; Cloudflare rate limiting is braces).
CACHE_TTL = 60
_cache: dict[int, tuple[float, bytes]] = {}


class Handler(BaseHTTPRequestHandler):
    db_path = DEFAULT_DB

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            with open(HTML_PATH, "rb") as f:
                body = f.read()
            self._send(200, "text/html; charset=utf-8", body)
        elif parsed.path.startswith("/static/"):
            # Page assets (theme.css). Served from app/static/ so the app is
            # self-contained; the page links them with relative URLs, which
            # keeps it working under a reverse-proxy subpath too.
            name = os.path.basename(parsed.path)
            path = os.path.join(ASSETS_DIR, name)
            ext = os.path.splitext(name)[1].lower()
            if os.path.isfile(path) and ext in ASSET_TYPES:
                with open(path, "rb") as f:
                    self._send(200, ASSET_TYPES[ext], f.read())
            else:
                self._send(404, "text/plain", b"not found")
        elif parsed.path == "/api/data":
            try:
                hours = int(parse_qs(parsed.query).get("hours", ["24"])[0])
            except ValueError:
                hours = 24
            hours = max(0, hours)
            cached = _cache.get(hours)
            if cached and cached[0] > time.monotonic():
                self._send(200, "application/json", cached[1])
                return
            try:
                payload = build_payload(self.db_path, hours)
                body = json.dumps(payload).encode("utf-8")
                _cache[hours] = (time.monotonic() + CACHE_TTL, body)
                self._send(200, "application/json", body)
            except sqlite3.Error as err:
                # log detail server-side only; never echo internals to clients
                print(f"api/data error: {err}", file=sys.stderr)
                self._send(
                    500,
                    "application/json",
                    json.dumps({"error": "data temporarily unavailable"}).encode("utf-8"),
                )
        else:
            self._send(404, "text/plain", b"not found")

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        pass  # keep the terminal quiet


def _is_dashboard(host: str, port: int) -> bool:
    """True if the process on host:port looks like this dashboard."""
    try:
        with urllib.request.urlopen(
            f"http://{host}:{port}/api/data?hours=1", timeout=2
        ) as resp:
            return resp.status == 200 and "thermostats" in resp.read(2048).decode(
                "utf-8", "replace"
            )
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite database (default: {DEFAULT_DB})")
    parser.add_argument("--port", type=int, default=8321)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-open", action="store_true", help="don't open a browser")
    args = parser.parse_args()

    db_path = os.path.expanduser(args.db)
    if not os.path.isfile(db_path):
        print(f"Database {db_path} not found — run ecobee_logger.py first.", file=sys.stderr)
        return 2

    Handler.db_path = db_path
    server = None
    for port in range(args.port, args.port + 10):
        try:
            server = ThreadingHTTPServer((args.host, port), Handler)
            break
        except OSError as err:
            if err.errno not in (errno.EADDRINUSE, errno.EACCES):
                raise
            if _is_dashboard(args.host, port):
                url = f"http://{args.host}:{port}/"
                print(f"Dashboard already running at {url}")
                if not args.no_open:
                    webbrowser.open(url)
                return 0
            print(f"Port {port} is in use by something else; trying {port + 1}...")
    if server is None:
        print(f"No free port in {args.port}-{args.port + 9}.", file=sys.stderr)
        return 1

    url = f"http://{args.host}:{server.server_address[1]}/"
    print(f"Serving ecobee dashboard at {url}  (Ctrl-C to stop)")
    if not args.no_open:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
