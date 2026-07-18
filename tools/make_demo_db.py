"""Generate a synthetic demo database — no ecobee account required.

Creates three fictional thermostats with 48 hours of plausible readings so
you can explore the dashboard (and take screenshots) before connecting a
real account. All data is generated from the model below; nothing here
comes from a real home.

Usage:
    python tools/make_demo_db.py                       # ./data/db/demo.sqlite3
    python tools/make_demo_db.py --db /tmp/demo.sqlite3 --hours 72
    python app/ecobee_dashboard.py --db ./data/db/demo.sqlite3
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
from schema import SCHEMA  # noqa: E402  (the real schema, no API client needed)

INTERVAL_S = 300

# Dehumidifier model. Humidity creeps back toward its outdoor-driven target at
# ~(target-rh)*0.04 per step (≈0.36 %/step at a 9-point gap), so removal must
# clearly exceed that for the unit to ever satisfy and shut off. 0.6 %/step
# with a 3-point hysteresis band gives ~45 min on / ~40 min off cycles.
DEHUM_RATE = 0.6    # %RH removed per 5-minute step while running
DEHUM_BAND = 3.0    # runs until this far below the setpoint, then stops

# Fictional homes. "identifier" mimics ecobee's 12-digit serial format but
# these are made-up numbers.
UNITS = [
    {
        "identifier": "100000000001", "name": "My ecobee", "display": "Lake House",
        "mode": "cool", "heat": 68.0, "cool": 74.0, "hum": 36.0, "dehum": 60.0,
        "base_temp": 73.0, "base_rh": 52.0, "equipment": ["compCool1", "fan"],
    },
    {
        "identifier": "100000000002", "name": "Main Floor", "display": None,
        "mode": "cool", "heat": 67.0, "cool": 76.0, "hum": 36.0, "dehum": 60.0,
        "base_temp": 75.5, "base_rh": 58.0, "equipment": ["fan"],
    },
    {
        "identifier": "100000000003", "name": "Upstairs", "display": None,
        "mode": "cool", "heat": 70.0, "cool": 72.0, "hum": 36.0, "dehum": 55.0,
        "base_temp": 71.5, "base_rh": 61.0, "equipment": ["compCool1", "fan", "dehumidifier"],
    },
]

SENSORS = {
    "100000000002": ["Kitchen", "Living Room"],
    "100000000003": ["Nursery"],
}
# Each room sits a bit warmer/cooler than the thermostat itself.
SENSOR_OFFSET = {"Kitchen": 1.4, "Living Room": -0.8, "Nursery": 1.1}


def diurnal(hour: float, low: float, high: float) -> float:
    """Outdoor curve: coolest ~5am, warmest ~3pm."""
    mid, amp = (low + high) / 2, (high - low) / 2
    return mid - amp * math.cos((hour - 5) / 24 * 2 * math.pi)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default="./data/db/demo.sqlite3")
    ap.add_argument("--hours", type=int, default=48)
    ap.add_argument("--seed", type=int, default=20260718, help="fixed seed = reproducible demo")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    db_path = os.path.expanduser(args.db)
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    if os.path.exists(db_path):
        os.remove(db_path)

    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA)
    db.execute(
        """INSERT OR IGNORE INTO meta (key, value)
           VALUES ('public_id_salt', lower(hex(randomblob(16))))"""
    )
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    steps = int(args.hours * 3600 / INTERVAL_S)
    start = now - timedelta(seconds=steps * INTERVAL_S)

    for u in UNITS:
        if u["display"]:
            db.execute(
                "INSERT OR REPLACE INTO unit_prefs (identifier, display_name) VALUES (?,?)",
                (u["identifier"], u["display"]),
            )

    # Smooth wander instead of independent per-sample noise: real thermostat
    # traces drift, they don't jitter. Each walk is a small bounded random walk.
    walks = {"out_t": 0.0, "out_rh": 0.0}
    for u in UNITS:
        walks[f"t:{u['identifier']}"] = 0.0
    for names in SENSORS.values():
        for n in names:
            walks[f"s:{n}"] = 0.0
    rh_state: dict[str, float] = {}   # humidity + dehumidifier state per unit

    def wander(key: str, step: float, limit: float) -> float:
        v = walks[key] + rng.uniform(-step, step)
        walks[key] = max(-limit, min(limit, v))
        return walks[key]

    for i in range(steps + 1):
        ts = start + timedelta(seconds=i * INTERVAL_S)
        ts_s = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        hour = ts.hour + ts.minute / 60
        out_t = round(diurnal(hour, 64, 88) + wander("out_t", 0.10, 1.2), 1)
        out_rh = round(max(30, min(96, 150 - out_t + wander("out_rh", 0.35, 5.0))))

        for u in UNITS:
            # Indoor lags outdoor a little; cooling pulls it back toward setpoint.
            drift = (out_t - u["base_temp"]) * 0.06
            temp = (u["base_temp"] + drift + math.sin(i / 30) * 0.35
                    + wander(f"t:{u['identifier']}", 0.04, 0.35))
            cooling = temp > u["cool"] - 0.3
            if cooling:
                temp -= 0.6
            # Humidity is a *state*, not a formula: it creeps toward the level
            # implied by outdoor conditions, and the dehumidifier actively
            # pulls it down. With hysteresis (on above setpoint, off ~2 points
            # below) that produces the sawtooth a real dehumidifier makes.
            rh_key, dh_key = f"rh:{u['identifier']}", f"dh:{u['identifier']}"
            target = u["base_rh"] + (out_rh - u["base_rh"]) * 0.10
            rh = rh_state.get(rh_key, target)
            rh += (target - rh) * 0.04 + rng.uniform(-0.10, 0.10)

            dehum_on = False
            if "dehumidifier" in u["equipment"]:
                was_on = rh_state.get(dh_key, 0.0) > 0.5
                dehum_on = rh > (u["dehum"] - DEHUM_BAND) if was_on else rh > u["dehum"]
                rh_state[dh_key] = 1.0 if dehum_on else 0.0
                if dehum_on:
                    rh -= DEHUM_RATE
            rh_state[rh_key] = rh

            running = []
            if cooling and "compCool1" in u["equipment"]:
                running += ["compCool1", "fan"]
            if dehum_on:
                running += ["dehumidifier"] + ([] if "fan" in running else ["fan"])
            if not running and "fan" in u["equipment"] and rng.random() < 0.12:
                running = ["fan"]

            db.execute(
                """INSERT INTO thermostat_readings (
                       ts_utc, identifier, name, connected, hvac_mode, current_climate,
                       equipment_status, actual_temp_f, actual_humidity, desired_heat_f,
                       desired_cool_f, desired_humidity, desired_dehumidity,
                       desired_fan_mode, fan_min_on_time, active_event_type,
                       active_event_name, outdoor_temp_f, outdoor_humidity,
                       outdoor_condition, raw_json
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ts_s, u["identifier"], u["name"], 1, u["mode"],
                    "sleep" if hour < 6 or hour >= 22 else "home",
                    ",".join(running), round(temp, 1), round(rh),
                    u["heat"], u["cool"], u["hum"], u["dehum"], "auto", 5,
                    None, None, out_t, out_rh, "Partly Cloudy", None,
                ),
            )

            for name in SENSORS.get(u["identifier"], []):
                db.execute(
                    """INSERT INTO sensor_readings (
                           ts_utc, thermostat_identifier, sensor_id, sensor_name,
                           sensor_type, in_use, temperature_f, humidity, occupancy
                       ) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        ts_s, u["identifier"], f"rs:{name}", name,
                        "ecobee3_remote_sensor", 1,
                        round(temp + SENSOR_OFFSET[name]
                              + wander(f"s:{name}", 0.05, 0.5), 1), None,
                        1 if 7 <= hour < 22 and rng.random() < 0.55 else 0,
                    ),
                )

    db.commit()
    rows = db.execute("SELECT COUNT(*) FROM thermostat_readings").fetchone()[0]
    db.close()
    print(f"Wrote {rows} synthetic readings for {len(UNITS)} thermostats to {db_path}")
    print(f"View with:  python app/ecobee_dashboard.py --db {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
