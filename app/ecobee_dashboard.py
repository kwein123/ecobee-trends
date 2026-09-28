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
import collections
import errno
import gzip
import hashlib
import json
import math
import os
import pathlib
import secrets
import sqlite3
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

DEFAULT_DB = os.environ.get("ECOBEE_DB", "./data/db/ecobee.sqlite3")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(SCRIPT_DIR, "ecobee_dashboard.html")
ASSETS_DIR = os.path.join(SCRIPT_DIR, "static")
ASSET_TYPES = {".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
               ".png": "image/png", ".ico": "image/x-icon", ".jpg": "image/jpeg",
               ".webp": "image/webp", ".woff2": "font/woff2",
               ".js": "text/javascript; charset=utf-8"}
# Optional folder that brands the page for the site hosting it (see README,
# "Hosting it under your own site"). All files in it are optional:
#   head.html    inserted into <head> (extra stylesheets, favicon links)
#   header.html  replaces the page's default site header
#   anything else with an ASSET_TYPES extension is served at site/<name>
DEFAULT_SITE_DIR = os.environ.get("ECOBEE_SITE_DIR") or None


def _read_optional(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def render_page(site_dir: Optional[str]) -> bytes:
    """The dashboard HTML, with the site folder's head/header applied."""
    with open(HTML_PATH, encoding="utf-8") as f:
        html = f.read()
    if site_dir:
        head = _read_optional(os.path.join(site_dir, "head.html"))
        if head is not None:
            html = html.replace("<!-- site:head -->", head.strip(), 1)
        header = _read_optional(os.path.join(site_dir, "header.html"))
        if header is not None:
            start, end = "<!-- site:header -->", "<!-- /site:header -->"
            i, j = html.index(start), html.index(end) + len(end)
            html = html[:i] + header.strip() + html[j:]
    return html.encode("utf-8")

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Measured values are averaged when readings are grouped into buckets.
AVERAGED_COLUMNS = ["actual_temp_f", "outdoor_temp_f", "actual_humidity", "outdoor_humidity"]
# Setpoints are step functions: a bucket shows the value in force at its end.
SETPOINT_COLUMNS = ["desired_heat_f", "desired_cool_f", "desired_humidity", "desired_dehumidity"]
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

# Longest range the API will serve (10 years). Anything larger is clamped, so
# a hostile ?hours= can't overflow date arithmetic.
MAX_HOURS = 24 * 366 * 10
# Upper bound on points per thermostat in one response. Longer ranges are
# grouped into buckets so a year of history costs about as much as a month.
MAX_POINTS = 800
# Bucket sizes to pick from (seconds). Round sizes keep bucket edges on
# familiar boundaries (quarter hours, hours, days).
BUCKET_SIZES = [600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800]
# At or below the logger's default interval, grouping would change nothing.
RAW_BUCKET_LIMIT = 300

# A row whose timestamp SQLite can't parse is skipped rather than allowed to
# break the whole response. (The logger never writes one; this is defensive.)
VALID_TS = "strftime('%s', ts_utc) IS NOT NULL"

# Used only if the database has no salt yet (an install from before the salt
# existed): identifiers stay masked, they just change when the server restarts.
_FALLBACK_SALT = secrets.token_hex(16)


def bucket_seconds(span_s: float) -> int:
    """Bucket size for a time span, or 0 to send every reading."""
    rough = span_s / MAX_POINTS
    if rough <= RAW_BUCKET_LIMIT:
        return 0
    for size in BUCKET_SIZES:
        if size >= rough:
            return size
    return int(math.ceil(rough / 86400.0)) * 86400


def mask_identifier(salt: str, identifier: str) -> str:
    """Stable, non-reversible public id for a thermostat serial number."""
    return hashlib.sha256((salt + identifier).encode("utf-8")).hexdigest()[:12]


def _round(value: Optional[float], digits: int = 1) -> Optional[float]:
    return None if value is None else round(value, digits)


def _connect_readonly(db_path: str) -> sqlite3.Connection:
    # as_uri() percent-encodes, so paths containing ?, # or spaces still work.
    uri = pathlib.Path(db_path).resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    return db


def build_payload(db_path: str, hours: int, now: Optional[datetime] = None) -> dict:
    """Readings for the last ``hours`` hours (0 = everything), per thermostat.

    Every known thermostat is included, even one with no readings in the
    range, so a unit that went offline shows up as stale instead of silently
    disappearing from the page.
    """
    now = now or datetime.now(timezone.utc)
    hours = max(0, min(int(hours), MAX_HOURS))
    cutoff = (now - timedelta(hours=hours)).strftime(TS_FORMAT) if hours else ""

    db = _connect_readonly(db_path)
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "thermostat_readings" not in tables:
            # Logger hasn't created the schema yet: an empty page, not an error.
            return _payload(now, hours, 0, 0, [])

        # A handful of thermostats and a large table: every query below is
        # per thermostat so it can seek the (identifier, ts_utc) index
        # instead of scanning all history.
        idents = _identifiers(db)
        if hours:
            span_s = hours * 3600
        else:
            firsts = [
                row[0]
                for i in idents
                for row in db.execute(
                    f"""SELECT ts_utc FROM thermostat_readings
                        WHERE identifier = ? AND {VALID_TS} ORDER BY ts_utc LIMIT 1""",
                    (i,),
                )
            ]
            span_s = (now - _parse_ts(min(firsts))).total_seconds() if firsts else 0
        bucket = bucket_seconds(span_s)
        group = bucket or 1  # 1-second groups = one row per reading

        latest_rows = [
            row
            for i in idents
            for row in db.execute(
                f"""SELECT identifier, ts_utc, name, connected, hvac_mode, current_climate,
                           equipment_status, actual_temp_f, actual_humidity
                    FROM thermostat_readings WHERE identifier = ? AND {VALID_TS}
                    ORDER BY ts_utc DESC LIMIT 1""",
                (i,),
            )
        ]

        # Bare columns (the setpoints) come from the row holding MAX(ts_utc):
        # a documented SQLite guarantee when the query has a single max().
        series_sql = f"""
            SELECT identifier,
                   CAST(strftime('%s', ts_utc) AS INTEGER) / :g AS bucket,
                   MAX(ts_utc) AS last_ts,
                   {", ".join(SETPOINT_COLUMNS)},
                   AVG(CAST(strftime('%s', ts_utc) AS INTEGER)) AS epoch,
                   {", ".join(f"AVG({c}) AS {c}" for c in AVERAGED_COLUMNS)},
                   COUNT(*) AS n,
                   GROUP_CONCAT(COALESCE(equipment_status, ''), '|') AS equipment
            FROM thermostat_readings
            WHERE identifier = :ident AND ts_utc >= :cutoff
            GROUP BY bucket ORDER BY bucket"""
        series_rows = [
            row
            for i in idents
            for row in db.execute(series_sql, {"g": group, "cutoff": cutoff, "ident": i})
        ]

        sensor_rows = []
        if "sensor_readings" in tables:
            sensor_sql = """
                SELECT thermostat_identifier AS identifier, sensor_name,
                       CAST(strftime('%s', ts_utc) AS INTEGER) / :g AS bucket,
                       AVG(temperature_f) AS temperature_f
                FROM sensor_readings
                WHERE thermostat_identifier = :ident AND ts_utc >= :cutoff
                  AND (sensor_type IS NULL OR sensor_type != 'thermostat')
                GROUP BY sensor_name, bucket"""
            sensor_rows = [
                row
                for i in idents
                for row in db.execute(sensor_sql, {"g": group, "cutoff": cutoff, "ident": i})
            ]

        prefs = {}
        if "unit_prefs" in tables:
            prefs = {
                r["identifier"]: r
                for r in db.execute("SELECT identifier, display_name, sort_order FROM unit_prefs")
            }
        salt = ""
        if "meta" in tables:
            row = db.execute("SELECT value FROM meta WHERE key = 'public_id_salt'").fetchone()
            salt = row["value"] if row else ""
    finally:
        db.close()

    thermostats: dict[str, dict] = {}
    for row in latest_rows:
        thermostats[row["identifier"]] = {
            "identifier": row["identifier"],
            "name": row["name"],
            "ts": [],
            "series": {c: [] for c in READING_COLUMNS},
            "equipment": {},
            "sensors": [],
            "latest": {
                "ts": _ts_ms(row["ts_utc"]),
                "connected": bool(row["connected"]),
                "hvac_mode": row["hvac_mode"],
                "current_climate": row["current_climate"],
                "equipment_status": row["equipment_status"] or "",
                "actual_temp_f": _round(row["actual_temp_f"]),
                "actual_humidity": _round(row["actual_humidity"]),
            },
        }

    # Equipment: fraction of readings in each bucket that had it running
    # (0/1 when not grouped). Tokens are collected first, then padded out.
    duty: dict[str, list[dict[str, int]]] = collections.defaultdict(list)
    counts: dict[str, list[int]] = collections.defaultdict(list)
    bucket_index: dict[tuple[str, int], int] = {}
    total_readings = 0
    for row in series_rows:
        t = thermostats.get(row["identifier"])
        if t is None or row["bucket"] is None:  # unparseable ts_utc: skip, don't fail
            continue
        total_readings += row["n"]
        bucket_index[(row["identifier"], row["bucket"])] = len(t["ts"])
        epoch = row["epoch"] if bucket else row["bucket"]
        t["ts"].append(int(round(epoch * 1000)))
        for col in AVERAGED_COLUMNS + SETPOINT_COLUMNS:
            t["series"][col].append(_round(row[col]))
        on: dict[str, int] = collections.Counter()
        for status in row["equipment"].split("|"):
            for tok in set(filter(None, status.split(","))):
                on[tok] += 1
        duty[row["identifier"]].append(on)
        counts[row["identifier"]].append(row["n"])

    for ident, t in thermostats.items():
        per_bucket = duty.get(ident, [])
        toks = set().union(*per_bucket) if per_bucket else set()
        ordered = [tok for tok in EQUIPMENT_ORDER if tok in toks]
        ordered += sorted(toks - set(EQUIPMENT_ORDER))
        for tok in ordered:
            t["equipment"][tok] = [
                _fraction(on.get(tok, 0), n) for on, n in zip(per_bucket, counts[ident])
            ]

    # Remote (non-built-in) sensors, aligned to their thermostat's buckets.
    by_sensor: dict[tuple[str, str], dict[int, float]] = collections.defaultdict(dict)
    for row in sensor_rows:
        idx = bucket_index.get((row["identifier"], row["bucket"]))
        if idx is not None:
            by_sensor[(row["identifier"], row["sensor_name"] or "Sensor")][idx] = _round(
                row["temperature_f"]
            )
    for (ident, sensor_name), values in sorted(by_sensor.items()):
        t = thermostats[ident]
        t["sensors"].append(
            {"name": sensor_name, "temperature_f": [values.get(i) for i in range(len(t["ts"]))]}
        )

    for t in thermostats.values():
        p = prefs.get(t["identifier"])
        t["display_name"] = (p["display_name"] if p else None) or t["name"]
        t["_sort"] = p["sort_order"] if p and p["sort_order"] is not None else None

    ordered_list = sorted(
        thermostats.values(),
        key=lambda t: (t["_sort"] is None, t["_sort"] or 0, (t["display_name"] or "").lower()),
    )
    salt = salt or _FALLBACK_SALT
    for t in ordered_list:
        del t["_sort"]
        # The real identifier is the thermostat's serial number. Replace it
        # with a salted hash: stable for client-side keying (panel order,
        # toggle prefs), but the serial itself never leaves the server.
        t["identifier"] = mask_identifier(salt, t["identifier"])

    return _payload(now, hours, bucket, total_readings, ordered_list)


def _identifiers(db: sqlite3.Connection) -> list[str]:
    """Distinct thermostat identifiers via index seeks (a "loose index scan").

    SELECT DISTINCT would read every index entry; this touches one per unit.
    """
    idents: list[str] = []
    while True:
        row = db.execute(
            "SELECT identifier FROM thermostat_readings WHERE identifier > ? "
            "ORDER BY identifier LIMIT 1",
            (idents[-1] if idents else "",),
        ).fetchone()
        if row is None:
            return idents
        idents.append(row[0])


def _payload(now: datetime, hours: int, bucket: int, readings: int, thermostats: list) -> dict:
    return {
        "generated_at": now.strftime(TS_FORMAT),
        "hours": hours,
        "bucket_s": bucket,
        "readings": readings,
        "thermostats": thermostats,
    }


def _fraction(on: int, n: int):
    if on == 0:
        return 0
    if on == n:
        return 1
    return round(on / n, 2)


def _parse_ts(ts_utc: str) -> datetime:
    return datetime.strptime(ts_utc, TS_FORMAT).replace(tzinfo=timezone.utc)


def _ts_ms(ts_utc: str) -> int:
    return int(_parse_ts(ts_utc).timestamp() * 1000)


class ResponseCache:
    """Small LRU of encoded /api/data responses.

    The logger only writes every few minutes, so identical responses within
    the TTL are free. Size-bounded so varying ?hours= can't grow memory.
    """

    def __init__(self, ttl: float = 60, max_entries: int = 8):
        self.ttl = ttl
        self.max_entries = max_entries
        self._items: "collections.OrderedDict[int, tuple[float, bytes, bytes]]" = (
            collections.OrderedDict()
        )
        self._lock = threading.Lock()

    def get(self, key: int) -> Optional[tuple[bytes, bytes]]:
        with self._lock:
            item = self._items.get(key)
            if not item or item[0] <= time.monotonic():
                return None
            self._items.move_to_end(key)
            return item[1], item[2]

    def put(self, key: int, body: bytes, gz: bytes) -> None:
        with self._lock:
            self._items[key] = (time.monotonic() + self.ttl, body, gz)
            self._items.move_to_end(key)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        return len(self._items)


CACHE = ResponseCache()

PAGE_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def parse_hours(query: str) -> int:
    try:
        hours = int(parse_qs(query).get("hours", ["24"])[0])
    except ValueError:  # includes absurdly long digit strings
        return 24
    return max(0, min(hours, MAX_HOURS))


class Handler(BaseHTTPRequestHandler):
    db_path = DEFAULT_DB
    site_dir = DEFAULT_SITE_DIR
    server_version = "ecobee-trends"
    sys_version = ""
    # Drop connections that stall mid-request instead of holding a thread.
    timeout = 30

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        self._dispatch(head=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch(head=True)

    def _dispatch(self, head: bool) -> None:
        self._head = head
        try:
            self._route(urlparse(self.path))
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away
        except Exception as err:  # never drop a connection without a response
            print(f"{self.command} {self.path} failed: {err!r}", file=sys.stderr)
            self._send_json(500, {"error": "internal error"})

    def _route(self, parsed) -> None:
        if parsed.path in ("/", "/index.html"):
            self._send_body(render_page(self.site_dir), "text/html; charset=utf-8", csp=True)
        elif parsed.path.startswith("/static/"):
            # Page assets, served from app/static/. The page links them with
            # relative URLs, so it also works under a reverse-proxy subpath.
            self._send_asset(ASSETS_DIR, parsed.path)
        elif parsed.path.startswith("/site/") and self.site_dir:
            self._send_asset(self.site_dir, parsed.path)
        elif parsed.path == "/api/data":
            self._send_data(parse_hours(parsed.query))
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")

    def _send_data(self, hours: int) -> None:
        cached = CACHE.get(hours)
        if cached is None:
            try:
                payload = build_payload(self.db_path, hours)
            except sqlite3.Error as err:
                # log detail server-side only; never echo internals to clients
                print(f"api/data error: {err}", file=sys.stderr)
                self._send_json(500, {"error": "data temporarily unavailable"})
                return
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            cached = (body, gzip.compress(body, compresslevel=6))
            CACHE.put(hours, *cached)
        self._send(200, "application/json", cached[0], gz=cached[1])

    def _send_asset(self, directory: str, url_path: str) -> None:
        # basename() confines lookups to that one directory; only known file
        # types are served, so head.html/header.html stay unreachable.
        name = os.path.basename(url_path)
        ext = os.path.splitext(name)[1].lower()
        path = os.path.join(directory, name)
        if name and ext in ASSET_TYPES and os.path.isfile(path):
            with open(path, "rb") as f:
                self._send_body(f.read(), ASSET_TYPES[ext])
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")

    def _send_body(self, body: bytes, content_type: str, csp: bool = False) -> None:
        etag = '"' + hashlib.sha256(body).hexdigest()[:20] + '"'
        extra = {"ETag": etag, "Cache-Control": "no-cache"}
        if csp:
            extra["Content-Security-Policy"] = PAGE_CSP
        if self.headers.get("If-None-Match") == etag:
            self._send(304, content_type, b"", extra=extra)
            return
        gz = gzip.compress(body, compresslevel=6) if len(body) > 1024 else None
        self._send(200, content_type, body, gz=gz, extra=extra)

    def _send_json(self, status: int, obj: dict) -> None:
        self._send(status, "application/json", json.dumps(obj).encode("utf-8"))

    def _send(self, status: int, content_type: str, body: bytes,
              gz: Optional[bytes] = None, extra: Optional[dict] = None) -> None:
        headers = {
            "Content-Type": content_type,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        }
        headers.update(extra or {})
        if gz is not None:
            headers["Vary"] = "Accept-Encoding"
            if "gzip" in self.headers.get("Accept-Encoding", ""):
                body = gz
                headers["Content-Encoding"] = "gzip"
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        if status != 304:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not getattr(self, "_head", False) and status != 304:
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
    parser.add_argument(
        "--site-dir", default=DEFAULT_SITE_DIR,
        help="folder of optional branding files: head.html, header.html, images "
             "(default: $ECOBEE_SITE_DIR)",
    )
    args = parser.parse_args()

    db_path = os.path.expanduser(args.db)
    if not os.path.isfile(db_path):
        print(f"Database {db_path} not found — run ecobee_logger.py first.", file=sys.stderr)
        return 2

    Handler.db_path = db_path
    if args.site_dir:
        Handler.site_dir = os.path.abspath(os.path.expanduser(args.site_dir))
        if not os.path.isdir(Handler.site_dir):
            print(f"Site folder {Handler.site_dir} not found.", file=sys.stderr)
            return 2
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
