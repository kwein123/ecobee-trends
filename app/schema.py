"""SQLite schema shared by the logger, the dashboard, and the demo generator.

Kept in its own module (with no third-party imports) so tools that only need
the table definitions don't have to pull in the ecobee API client.
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS thermostat_readings (
    id INTEGER PRIMARY KEY,
    ts_utc TEXT NOT NULL,
    identifier TEXT NOT NULL,
    name TEXT,
    connected INTEGER,
    hvac_mode TEXT,
    current_climate TEXT,
    equipment_status TEXT,
    actual_temp_f REAL,
    actual_humidity REAL,
    desired_heat_f REAL,
    desired_cool_f REAL,
    desired_humidity REAL,
    desired_dehumidity REAL,
    desired_fan_mode TEXT,
    fan_min_on_time INTEGER,
    active_event_type TEXT,
    active_event_name TEXT,
    outdoor_temp_f REAL,
    outdoor_humidity REAL,
    outdoor_condition TEXT,
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_readings_ident_ts
    ON thermostat_readings (identifier, ts_utc);

-- Presentation-only overrides: rename/reorder units on the dashboard
-- without touching the logged data.
CREATE TABLE IF NOT EXISTS unit_prefs (
    identifier TEXT PRIMARY KEY,
    display_name TEXT,
    sort_order INTEGER
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS sensor_readings (
    id INTEGER PRIMARY KEY,
    ts_utc TEXT NOT NULL,
    thermostat_identifier TEXT NOT NULL,
    sensor_id TEXT,
    sensor_name TEXT,
    sensor_type TEXT,
    in_use INTEGER,
    temperature_f REAL,
    humidity REAL,
    occupancy INTEGER
);
CREATE INDEX IF NOT EXISTS ix_sensors_ident_ts
    ON sensor_readings (thermostat_identifier, ts_utc);
"""
