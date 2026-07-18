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
    python scripts/ecobee_logger.py

    # poll forever every 5 minutes:
    python scripts/ecobee_logger.py --interval 300

    # also keep the full raw API JSON per reading (larger database):
    python scripts/ecobee_logger.py --interval 300 --raw

Query examples:
    sqlite3 ~/.ecobee/ecobee.sqlite3 \
      "SELECT ts_utc, name, actual_temp_f, actual_humidity, equipment_status
       FROM thermostat_readings ORDER BY ts_utc DESC LIMIT 10;"
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Optional

# Allow running from a plain checkout without installing the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyecobee import Ecobee
from pyecobee.errors import EcobeeError, InvalidTokenError

from schema import SCHEMA  # table definitions (shared with tools/make_demo_db.py)

_LOGGER = logging.getLogger("ecobee_logger")

DEFAULT_CONFIG = os.path.expanduser("~/.ecobee/ecobee.conf")
DEFAULT_DB = os.path.expanduser("~/.ecobee/ecobee.sqlite3")

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
    for thermostat in ecobee.thermostats:
        summary = record_thermostat(cur, ts, thermostat, keep_raw)
        _LOGGER.info(summary)
    db.commit()


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
        "ecobee only updates runtime data every few minutes, so 300 is a "
        "sensible floor.",
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

    config_path = os.path.expanduser(args.config)
    if not os.path.isfile(config_path):
        _LOGGER.error(
            "Config file %s not found. Run scripts/ecobee_login.py first.", config_path
        )
        return 2

    db_path = os.path.expanduser(args.db)
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

    ecobee = Ecobee(config_filename=config_path)
    ecobee.read_config_from_file()

    while True:
        try:
            poll_once(ecobee, db, args.raw)
        except InvalidTokenError:
            _LOGGER.error(
                "ecobee tokens are no longer valid; run scripts/ecobee_login.py "
                "to re-authenticate."
            )
            return 3
        except KeyboardInterrupt:
            return 0
        except Exception as err:  # keep a long-running loop alive on transient errors
            if not args.interval:
                raise
            _LOGGER.warning("Poll failed (%s); retrying in %ss", err, args.interval)

        if not args.interval:
            return 0
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    sys.exit(main())
