"""Poll ecobee thermostats and record readings to a SQLite database.

Records, per thermostat: HVAC mode, current program climate, running
equipment (furnace, compressor, fan, humidifier, ...), indoor temperature
and humidity, heat/cool setpoints, active hold/vacation events, and outdoor
weather as reported by ecobee. Also records every remote sensor's
temperature, humidity, and occupancy.

All thermostats registered to the account are captured in each poll,
regardless of which property they are in. Run ``ecobee_login.py`` once
first to create the token config file.

Usage:
    # single poll (suitable for cron / launchd):
    python app/ecobee_logger.py

    # poll forever every 5 minutes:
    python app/ecobee_logger.py --interval 300

    # also keep the full raw API JSON per reading (larger database):
    python app/ecobee_logger.py --interval 300 --raw

Query examples:
    sqlite3 data/db/ecobee.sqlite3 \
      "SELECT ts_utc, name, actual_temp_f, actual_humidity, equipment_status
       FROM thermostat_readings ORDER BY ts_utc DESC LIMIT 10;"
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from typing import Optional

# Allow running from a plain checkout without installing the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyecobee import Ecobee
from pyecobee.errors import EcobeeError, InvalidTokenError

from schema import SCHEMA  # table definitions (shared with tools/make_demo_db.py)

_LOGGER = logging.getLogger("ecobee_logger")

DEFAULT_CONFIG = os.environ.get("ECOBEE_CONFIG", "./data/auth/ecobee.conf")
DEFAULT_DB = os.environ.get("ECOBEE_DB", "./data/db/ecobee.sqlite3")

# ecobee asks API clients not to poll more often than every 3 minutes; its
# cloud copy of thermostat data only changes that often anyway.
MIN_INTERVAL = 60
RECOMMENDED_MIN_INTERVAL = 180
# While waiting for a re-login, how often to look for a new token file.
TOKEN_WAIT_CHECK = 60

# ecobee sentinel for a sensor that has no reading yet.
ECOBEE_UNKNOWN_VALUES = {"unknown", "-5002", "-5003", ""}


def tenths_f(value) -> Optional[float]:
    """Convert an ecobee tenths-of-°F value to °F, or None if unknown."""
    if value is None or str(value).strip() in ECOBEE_UNKNOWN_VALUES:
        return None
    try:
        return int(value) / 10.0
    except (TypeError, ValueError):
        return None


def number(value) -> Optional[float]:
    if value is None or str(value).strip() in ECOBEE_UNKNOWN_VALUES:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def active_event(thermostat: dict) -> tuple[Optional[str], Optional[str]]:
    for event in thermostat.get("events") or []:
        if event.get("running"):
            return event.get("type"), event.get("name")
    return None, None


def outdoor_weather(thermostat: dict) -> tuple[Optional[float], Optional[float], Optional[str]]:
    forecasts = (thermostat.get("weather") or {}).get("forecasts") or []
    if not forecasts:
        return None, None, None
    current = forecasts[0]
    return (
        tenths_f(current.get("temperature")),
        number(current.get("relativeHumidity")),
        current.get("condition"),
    )


def record_thermostat(cur: sqlite3.Cursor, ts: str, thermostat: dict, keep_raw: bool) -> str:
    runtime = thermostat.get("runtime") or {}
    settings = thermostat.get("settings") or {}
    program = thermostat.get("program") or {}
    event_type, event_name = active_event(thermostat)
    out_temp, out_hum, out_cond = outdoor_weather(thermostat)

    identifier = thermostat.get("identifier")
    name = thermostat.get("name")
    cur.execute(
        """INSERT INTO thermostat_readings (
               ts_utc, identifier, name, connected, hvac_mode, current_climate,
               equipment_status, actual_temp_f, actual_humidity, desired_heat_f,
               desired_cool_f, desired_humidity, desired_dehumidity,
               desired_fan_mode, fan_min_on_time, active_event_type,
               active_event_name, outdoor_temp_f, outdoor_humidity,
               outdoor_condition, raw_json
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            ts,
            identifier,
            name,
            1 if runtime.get("connected") else 0,
            settings.get("hvacMode"),
            program.get("currentClimateRef"),
            thermostat.get("equipmentStatus") or "",
            tenths_f(runtime.get("actualTemperature")),
            number(runtime.get("actualHumidity")),
            tenths_f(runtime.get("desiredHeat")),
            tenths_f(runtime.get("desiredCool")),
            number(runtime.get("desiredHumidity")),
            number(runtime.get("desiredDehumidity")),
            runtime.get("desiredFanMode"),
            settings.get("fanMinOnTime"),
            event_type,
            event_name,
            out_temp,
            out_hum,
            out_cond,
            json.dumps(thermostat) if keep_raw else None,
        ),
    )

    for sensor in thermostat.get("remoteSensors") or []:
        temp = hum = occ = None
        for cap in sensor.get("capability") or []:
            cap_type = cap.get("type")
            if cap_type == "temperature":
                temp = tenths_f(cap.get("value"))
            elif cap_type == "humidity":
                hum = number(cap.get("value"))
            elif cap_type == "occupancy":
                occ = 1 if str(cap.get("value")).lower() == "true" else 0
        cur.execute(
            """INSERT INTO sensor_readings (
                   ts_utc, thermostat_identifier, sensor_id, sensor_name,
                   sensor_type, in_use, temperature_f, humidity, occupancy
               ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                ts,
                identifier,
                sensor.get("id"),
                sensor.get("name"),
                sensor.get("type"),
                1 if sensor.get("inUse") else 0,
                temp,
                hum,
                occ,
            ),
        )

    running = thermostat.get("equipmentStatus") or "idle"
    temp_str = tenths_f(runtime.get("actualTemperature"))
    hum_str = number(runtime.get("actualHumidity"))
    return f"{name}: {temp_str}°F {hum_str}%RH mode={settings.get('hvacMode')} running=[{running}]"


def poll_once(ecobee: Ecobee, db: sqlite3.Connection, keep_raw: bool) -> None:
    if not ecobee.update():
        raise EcobeeError("thermostat fetch returned no data")

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = db.cursor()
    summaries = []
    try:
        for thermostat in ecobee.thermostats or []:
            if not thermostat.get("identifier"):
                _LOGGER.warning("Skipping a thermostat with no identifier")
                continue
            summaries.append(record_thermostat(cur, ts, thermostat, keep_raw))
        db.commit()
    except BaseException:
        # All of a poll or none of it: without this, rows from a failed poll
        # would sit in the open transaction and be committed by the next one.
        db.rollback()
        raise
    for summary in summaries:
        _LOGGER.info(summary)


def open_db(db_path: str) -> sqlite3.Connection:
    """Open (creating if needed) the readings database."""
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA journal_mode = WAL")  # concurrent reader (dashboard) friendly
    db.execute("PRAGMA busy_timeout = 5000")
    db.executescript(SCHEMA)
    # One-time random salt: the dashboard hashes thermostat identifiers
    # (serial numbers) with it so the public API never exposes real serials.
    db.execute(
        """INSERT OR IGNORE INTO meta (key, value)
           VALUES ('public_id_salt', lower(hex(randomblob(16))))"""
    )
    db.commit()
    return db


def _mtime(path: str) -> Optional[int]:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def wait_for_new_config(config_path: str, sleep, stop: threading.Event) -> None:
    """Block until the token file is rewritten (someone ran the login again)
    or a stop is requested.

    Exiting instead would make Docker restart the logger in a loop, hitting
    ecobee's auth endpoint with a dead token every time.
    """
    before = _mtime(config_path)
    while _mtime(config_path) == before:
        if stop.is_set():
            return
        sleep(TOKEN_WAIT_CHECK)
    _LOGGER.info("Token file %s changed; resuming", config_path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help=f"token config file from ecobee_login.py (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--db",
        default=DEFAULT_DB,
        help=f"SQLite database to append readings to (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=0,
        metavar="SECONDS",
        help="poll repeatedly at this interval; omit to poll once and exit. "
        f"ecobee only updates runtime data every few minutes, so 300 is "
        f"sensible (minimum {MIN_INTERVAL}).",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="store the full thermostat JSON with each reading (much larger DB)",
    )
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.interval and args.interval < MIN_INTERVAL:
        _LOGGER.warning("--interval %s is too short; using %s", args.interval, MIN_INTERVAL)
        args.interval = MIN_INTERVAL
    elif args.interval and args.interval < RECOMMENDED_MIN_INTERVAL:
        _LOGGER.warning(
            "ecobee refreshes its data every few minutes; polling every %ss "
            "mostly records repeats", args.interval
        )

    config_path = os.path.expanduser(args.config)
    if not os.path.isfile(config_path):
        _LOGGER.error(
            "Config file %s not found. Run app/ecobee_login.py first.", config_path
        )
        return 2

    # As PID 1 in its container the logger gets no default SIGTERM handling,
    # so `docker stop` would wait out its grace period and SIGKILL it. Just
    # flag the stop; run() acts on it between polls, never mid-transaction.
    stop = threading.Event()
    received: list[int] = []
    request_stop = stop_handler(stop, received)
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    db = open_db(os.path.expanduser(args.db))
    try:
        code = run(config_path, db, args.interval, args.raw, stop=stop)
    finally:
        db.close()
    if received:
        _LOGGER.info("Stopped by %s; database closed", signal.Signals(received[0]).name)
    return code


def stop_handler(stop: threading.Event, received: list):
    """A signal handler that asks run() to stop.

    It hands stop.set() to a helper thread rather than calling it directly:
    Python runs handlers on the main thread between any two steps, and if
    that happens while the main thread is inside stop.wait() it already
    holds the Event's (non-reentrant) lock, so calling set() here would
    deadlock. The helper thread simply waits its turn for the lock.
    """
    def request_stop(signum, frame):
        received.append(signum)
        threading.Thread(target=stop.set, daemon=True).start()
    return request_stop


def run(config_path: str, db: sqlite3.Connection, interval: int, keep_raw: bool,
        make_client=Ecobee, sleep=None, stop: Optional[threading.Event] = None) -> int:
    """The polling loop. Returns an exit code (single-poll mode, fatal, or 0
    once `stop` is set). By default it sleeps on `stop` so a stop cuts the
    wait short."""
    stop = stop or threading.Event()
    sleep = sleep or stop.wait
    ecobee = None
    while not stop.is_set():
        reason = None
        if ecobee is None:
            try:
                ecobee = make_client(config_filename=config_path)
                ecobee.read_config_from_file()
                if not isinstance(ecobee.config, dict):
                    raise ValueError("not a JSON object")
            except (OSError, KeyError, TypeError, ValueError) as err:
                ecobee = None
                reason = f"token file {config_path} is unreadable ({err!r})"
        if reason is None:
            try:
                poll_once(ecobee, db, keep_raw)
            except InvalidTokenError:
                reason = "ecobee tokens are no longer valid"
            except Exception as err:  # keep a long-running loop alive on transient errors
                if not interval:
                    raise
                _LOGGER.warning("Poll failed (%s); retrying in %ss", err, interval)

        if reason is not None:
            # Neither dead tokens nor an unreadable token file fixes itself;
            # only running the login again does.
            _LOGGER.error("%s; run app/ecobee_login.py to re-authenticate.", reason)
            if not interval:
                return 3
            _LOGGER.error("Waiting for a new token file before polling again.")
            wait_for_new_config(config_path, sleep, stop)
            ecobee = None
            continue

        if not interval:
            return 0
        sleep(interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
