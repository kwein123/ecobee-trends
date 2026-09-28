"""Shared fixtures for the test suite: temporary databases with known data.

Every identifier, name and value here is invented for the tests.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, ROOT)

from schema import SCHEMA  # noqa: E402

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def ts(dt: datetime) -> str:
    return dt.strftime(TS_FORMAT)


class TempDB:
    """A schema-initialised SQLite file in a temp dir, removed on close()."""

    def __init__(self, salt: str | None = "testsalt", schema: bool = True):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "ecobee.sqlite3")
        self.db = sqlite3.connect(self.path)
        if schema:
            self.db.executescript(SCHEMA)
            if salt is not None:
                self.db.execute("INSERT INTO meta (key, value) VALUES ('public_id_salt', ?)", (salt,))
            self.db.commit()

    def reading(self, when: datetime, identifier: str, name: str = "Unit", *,
                temp: float | None = 70.0, humidity: float | None = 45.0,
                heat: float | None = 68.0, cool: float | None = 75.0,
                dehum: float | None = 60.0, equipment: str = "", connected: int = 1,
                mode: str = "cool", climate: str = "home",
                out_temp: float | None = 80.0, out_hum: float | None = 50.0) -> None:
        self.db.execute(
            """INSERT INTO thermostat_readings (
                   ts_utc, identifier, name, connected, hvac_mode, current_climate,
                   equipment_status, actual_temp_f, actual_humidity, desired_heat_f,
                   desired_cool_f, desired_humidity, desired_dehumidity,
                   outdoor_temp_f, outdoor_humidity)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ts(when), identifier, name, connected, mode, climate, equipment, temp,
             humidity, heat, cool, 35.0, dehum, out_temp, out_hum),
        )

    def sensor(self, when: datetime, identifier: str, name: str, temp: float,
               sensor_type: str | None = "ecobee3_remote_sensor") -> None:
        self.db.execute(
            """INSERT INTO sensor_readings (ts_utc, thermostat_identifier, sensor_id,
                   sensor_name, sensor_type, in_use, temperature_f)
               VALUES (?,?,?,?,?,1,?)""",
            (ts(when), identifier, f"rs:{name}", name, sensor_type, temp),
        )

    def series(self, identifier: str, name: str, start: datetime, steps: int,
               interval_s: int = 300, **kw) -> None:
        for i in range(steps):
            self.reading(start + timedelta(seconds=i * interval_s), identifier, name, **kw)

    def pref(self, identifier: str, display_name: str | None = None, sort_order: int | None = None):
        self.db.execute(
            "INSERT OR REPLACE INTO unit_prefs (identifier, display_name, sort_order) VALUES (?,?,?)",
            (identifier, display_name, sort_order),
        )

    def commit(self) -> None:
        self.db.commit()

    def close(self) -> None:
        self.db.close()
        self.dir.cleanup()
