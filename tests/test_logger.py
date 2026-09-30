"""Tests for app/ecobee_logger.py and the token-file write in pyecobee."""

from __future__ import annotations

import copy
import json
import logging
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import helpers  # noqa: F401  (sets up sys.path)

import ecobee_logger as logger
from pyecobee.errors import InvalidTokenError
from pyecobee.util import config_from_file

logging.getLogger("ecobee_logger").setLevel(logging.CRITICAL)
logging.getLogger("pyecobee").setLevel(logging.CRITICAL)  # failures below are deliberate

# Shaped like one entry of ecobee's thermostatList; every value is invented.
THERMOSTAT = {
    "identifier": "100000000009",
    "name": "Test Unit",
    "equipmentStatus": "compCool1,fan",
    "runtime": {
        "connected": True,
        "actualTemperature": 725,
        "actualHumidity": 48,
        "desiredHeat": 680,
        "desiredCool": 740,
        "desiredHumidity": 36,
        "desiredDehumidity": 60,
        "desiredFanMode": "auto",
    },
    "settings": {"hvacMode": "cool", "fanMinOnTime": 5},
    "program": {"currentClimateRef": "home"},
    "events": [
        {"type": "hold", "name": "old", "running": False},
        {"type": "hold", "name": "auto", "running": True},
    ],
    "weather": {"forecasts": [{"temperature": 881, "relativeHumidity": 55, "condition": "Sunny"}]},
    "remoteSensors": [
        {"id": "rs:100", "name": "Bedroom", "type": "ecobee3_remote_sensor", "inUse": True,
         "capability": [{"type": "temperature", "value": "701"},
                        {"type": "occupancy", "value": "true"}]},
        {"id": "ei:0", "name": "Test Unit", "type": "thermostat", "inUse": False,
         "capability": [{"type": "temperature", "value": "725"},
                        {"type": "humidity", "value": "48"}]},
        {"id": "rs:101", "name": "Garage", "type": "ecobee3_remote_sensor", "inUse": False,
         "capability": [{"type": "temperature", "value": "unknown"}]},
    ],
}


class FakeEcobee:
    """Stands in for pyecobee.Ecobee; `script` lists what each update() does."""

    def __init__(self, script, thermostats=None, config_filename=None):
        self.script = script  # shared, so a re-created client continues it
        self.thermostats = thermostats if thermostats is not None else [copy.deepcopy(THERMOSTAT)]
        self.config = {"ACCESS_TOKEN": "x"}
        self.config_filename = config_filename

    def read_config_from_file(self):
        with open(self.config_filename) as f:
            self.config = json.load(f)

    def update(self):
        step = self.script.pop(0) if self.script else True
        if callable(step):
            step = step()
        if isinstance(step, BaseException):
            raise step
        return step


class StopLoop(Exception):
    pass


class Conversions(unittest.TestCase):
    def test_tenths_and_sentinels(self):
        self.assertEqual(logger.tenths_f(725), 72.5)
        self.assertEqual(logger.tenths_f("701"), 70.1)
        self.assertEqual(logger.tenths_f(-50), -5.0)
        for bad in (None, "unknown", "-5002", -5002, "-5003", "", "  ", "abc"):
            self.assertIsNone(logger.tenths_f(bad), bad)
        self.assertEqual(logger.number("48"), 48.0)
        self.assertIsNone(logger.number("unknown"))

    def test_active_event_and_weather(self):
        self.assertEqual(logger.active_event(THERMOSTAT), ("hold", "auto"))
        self.assertEqual(logger.active_event({}), (None, None))
        self.assertEqual(logger.outdoor_weather(THERMOSTAT), (88.1, 55.0, "Sunny"))
        self.assertEqual(logger.outdoor_weather({"weather": {}}), (None, None, None))


class Polling(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = logger.open_db(os.path.join(self.dir.name, "db", "ecobee.sqlite3"))

    def tearDown(self):
        self.db.close()
        self.dir.cleanup()

    def count(self, table="thermostat_readings"):
        return self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_open_db_is_idempotent_and_salt_is_stable(self):
        salt = self.db.execute("SELECT value FROM meta WHERE key='public_id_salt'").fetchone()[0]
        self.assertEqual(len(salt), 32)
        again = logger.open_db(os.path.join(self.dir.name, "db", "ecobee.sqlite3"))
        self.assertEqual(
            again.execute("SELECT value FROM meta WHERE key='public_id_salt'").fetchone()[0], salt)
        again.close()

    def test_poll_records_a_thermostat_and_its_sensors(self):
        logger.poll_once(FakeEcobee([True]), self.db, keep_raw=False)
        row = self.db.execute(
            """SELECT identifier, name, connected, hvac_mode, current_climate, equipment_status,
                      actual_temp_f, actual_humidity, desired_heat_f, desired_cool_f,
                      active_event_type, active_event_name, outdoor_temp_f, raw_json
               FROM thermostat_readings""").fetchone()
        self.assertEqual(row, ("100000000009", "Test Unit", 1, "cool", "home", "compCool1,fan",
                               72.5, 48.0, 68.0, 74.0, "hold", "auto", 88.1, None))
        sensors = self.db.execute(
            "SELECT sensor_name, sensor_type, temperature_f, occupancy FROM sensor_readings "
            "ORDER BY sensor_name").fetchall()
        self.assertEqual(sensors, [("Bedroom", "ecobee3_remote_sensor", 70.1, 1),
                                   ("Garage", "ecobee3_remote_sensor", None, None),
                                   ("Test Unit", "thermostat", 72.5, None)])

    def test_raw_json_only_when_asked(self):
        logger.poll_once(FakeEcobee([True]), self.db, keep_raw=True)
        raw = self.db.execute("SELECT raw_json FROM thermostat_readings").fetchone()[0]
        self.assertEqual(json.loads(raw)["identifier"], "100000000009")

    def test_sparse_thermostat_does_not_crash(self):
        logger.poll_once(FakeEcobee([True], [{"identifier": "100000000010"}]), self.db, False)
        self.assertEqual(self.count(), 1)

    def test_thermostat_without_identifier_is_skipped(self):
        good = copy.deepcopy(THERMOSTAT)
        logger.poll_once(FakeEcobee([True], [{"name": "?"}, good]), self.db, False)
        self.assertEqual(self.count(), 1)

    def test_failed_fetch_writes_nothing(self):
        with self.assertRaises(logger.EcobeeError):
            logger.poll_once(FakeEcobee([False]), self.db, False)
        self.assertEqual(self.count(), 0)

    def test_failure_mid_poll_rolls_back(self):
        second = copy.deepcopy(THERMOSTAT)
        second["identifier"] = "100000000011"
        client = FakeEcobee([True, True], [copy.deepcopy(THERMOSTAT), second])
        real = logger.record_thermostat
        calls = []

        def flaky(cur, ts, thermostat, keep_raw):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("disk hiccup")
            return real(cur, ts, thermostat, keep_raw)

        with mock.patch.object(logger, "record_thermostat", flaky):
            with self.assertRaises(RuntimeError):
                logger.poll_once(client, self.db, False)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.count("sensor_readings"), 0)
        # The next good poll must not drag the failed poll's rows in with it.
        logger.poll_once(client, self.db, False)
        self.assertEqual(self.count(), 2)
        self.assertEqual(len(set(r[0] for r in self.db.execute(
            "SELECT ts_utc FROM thermostat_readings"))), 1)


class RunLoop(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.config = os.path.join(self.dir.name, "ecobee.conf")
        with open(self.config, "w") as f:
            json.dump({"API_KEY": None, "ACCESS_TOKEN": "a", "REFRESH_TOKEN": "r"}, f)
        self.db = logger.open_db(os.path.join(self.dir.name, "ecobee.sqlite3"))

    def tearDown(self):
        self.db.close()
        self.dir.cleanup()

    def make(self, *script):
        clients = []
        steps = list(script)

        def factory(config_filename):
            c = FakeEcobee(steps, config_filename=config_filename)
            clients.append(c)
            return c
        return factory, clients

    def rows(self):
        return self.db.execute("SELECT COUNT(*) FROM thermostat_readings").fetchone()[0]

    def test_single_poll(self):
        factory, _ = self.make(True)
        self.assertEqual(logger.run(self.config, self.db, 0, False, make_client=factory), 0)
        self.assertEqual(self.rows(), 1)

    def test_single_poll_with_dead_tokens_exits_3(self):
        factory, _ = self.make(InvalidTokenError("dead"))
        self.assertEqual(logger.run(self.config, self.db, 0, False, make_client=factory), 3)

    def test_unreadable_token_file(self):
        with open(self.config, "w") as f:
            f.write('{"ACCESS_TOK')  # cut off mid-write
        factory, _ = self.make(True)
        self.assertEqual(logger.run(self.config, self.db, 0, False, make_client=factory), 3)

    def test_transient_errors_keep_the_loop_alive(self):
        factory, _ = self.make(RuntimeError("network"), False, True)
        sleeps = []

        def sleep(s):
            sleeps.append(s)
            if len(sleeps) == 3:
                raise StopLoop

        with self.assertRaises(StopLoop):
            logger.run(self.config, self.db, 300, False, make_client=factory, sleep=sleep)
        self.assertEqual(sleeps, [300, 300, 300])
        self.assertEqual(self.rows(), 1)

    def test_dead_tokens_wait_for_a_new_login_instead_of_exiting(self):
        factory, clients = self.make(InvalidTokenError("dead"), True)
        waits = []

        def sleep(s):
            waits.append(s)
            if s == logger.TOKEN_WAIT_CHECK and len(waits) == 2:
                # Simulate the user re-running the login: the file changes.
                st = os.stat(self.config)
                os.utime(self.config, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
            if s == 300:
                raise StopLoop

        with self.assertRaises(StopLoop):
            logger.run(self.config, self.db, 300, False, make_client=factory, sleep=sleep)
        self.assertEqual(waits, [logger.TOKEN_WAIT_CHECK, logger.TOKEN_WAIT_CHECK, 300])
        self.assertEqual(len(clients), 2, "the token file should be re-read after it changes")
        self.assertEqual(self.rows(), 1)

    def stop_soon(self, stop, waits):
        """A sleep that has a stop arrive shortly after it starts waiting."""
        def sleep(s):
            waits.append(s)
            threading.Timer(0.05, stop.set).start()
            return stop.wait(s)
        return sleep

    def test_stop_during_sleep_returns_promptly(self):
        factory, _ = self.make(True, True)
        stop, waits = threading.Event(), []
        start = time.monotonic()
        code = logger.run(self.config, self.db, 300, False, make_client=factory,
                          sleep=self.stop_soon(stop, waits), stop=stop)
        self.assertEqual(code, 0)
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(waits, [300])
        self.assertEqual(self.rows(), 1)

    def test_stop_during_a_poll_lets_it_commit_first(self):
        stop = threading.Event()

        def fetch_then_signal():
            stop.set()  # arrives while the poll is in flight
            return True

        factory, _ = self.make(fetch_then_signal, True)
        start = time.monotonic()
        code = logger.run(self.config, self.db, 300, False, make_client=factory, stop=stop)
        self.assertEqual(code, 0)
        self.assertLess(time.monotonic() - start, 5)
        self.assertFalse(self.db.in_transaction)
        # Committed, not just visible on this connection, and no second poll.
        other = sqlite3.connect(os.path.join(self.dir.name, "ecobee.sqlite3"))
        self.assertEqual(other.execute("SELECT COUNT(*) FROM thermostat_readings").fetchone()[0], 1)
        other.close()

    def test_stop_while_waiting_for_a_new_token_file(self):
        with open(self.config, "w") as f:
            f.write("not a token file")
        factory, clients = self.make(True)
        stop, waits = threading.Event(), []
        code = logger.run(self.config, self.db, 300, False, make_client=factory,
                          sleep=self.stop_soon(stop, waits), stop=stop)
        self.assertEqual(code, 0)
        self.assertEqual(waits, [logger.TOKEN_WAIT_CHECK])
        self.assertEqual(self.rows(), 0)


@unittest.skipUnless(hasattr(signal, "SIGTERM") and os.name == "posix", "needs POSIX signals")
class Signals(unittest.TestCase):
    """The real script, as Docker runs it: SIGTERM/SIGINT must exit 0 quickly."""

    def check(self, sig):
        with tempfile.TemporaryDirectory() as d:
            config = os.path.join(d, "ecobee.conf")
            with open(config, "w") as f:
                f.write("not a token file")  # so it waits without touching the network
            proc = subprocess.Popen(
                [sys.executable, os.path.join(helpers.ROOT, "app", "ecobee_logger.py"),
                 "--config", config, "--db", os.path.join(d, "ecobee.sqlite3"),
                 "--interval", "300"],
                stderr=subprocess.PIPE, text=True)
            try:
                lines, waiting = [], threading.Event()

                def read_until_waiting():
                    for line in proc.stderr:
                        lines.append(line)
                        if "Waiting for a new token file" in line:
                            waiting.set()
                            return

                # A time limit, so a logger that never gets there fails the
                # test instead of hanging CI.
                threading.Thread(target=read_until_waiting, daemon=True).start()
                self.assertTrue(waiting.wait(10),
                                "logger never started waiting:\n" + "".join(lines))
                proc.send_signal(sig)
                _, rest = proc.communicate(timeout=10)
            finally:
                proc.kill()
                proc.wait()
            self.assertEqual(proc.returncode, 0, "".join(lines) + rest)
            self.assertIn(f"Stopped by {sig.name}", rest)

    def test_sigterm(self):
        self.check(signal.SIGTERM)

    def test_handler_cannot_deadlock_on_the_events_lock(self):
        # Force the unlucky timing: the signal is handled while the main
        # thread holds the Event's internal lock, as it briefly does inside
        # stop.wait(). A handler that called stop.set() directly would wait
        # forever on a lock its own thread holds.
        stop, received = threading.Event(), []
        handler = logger.stop_handler(stop, received)

        def unlucky_timing():
            with stop._cond:
                handler(signal.SIGTERM, None)

        t = threading.Thread(target=unlucky_timing, daemon=True)
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive(), "signal handler deadlocked on the Event's lock")
        self.assertTrue(stop.wait(5))
        self.assertEqual(received, [signal.SIGTERM])

    def test_sigint(self):
        self.check(signal.SIGINT)


class TokenFileWrite(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "ecobee.conf")

    def tearDown(self):
        self.dir.cleanup()

    def test_written_private_and_complete(self):
        self.assertTrue(config_from_file(self.path, {"REFRESH_TOKEN": "abc"}))
        self.assertEqual(config_from_file(self.path), {"REFRESH_TOKEN": "abc"})
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(os.listdir(self.dir.name), ["ecobee.conf"])

    def test_failed_write_leaves_the_old_tokens(self):
        config_from_file(self.path, {"REFRESH_TOKEN": "old"})
        with mock.patch("pyecobee.util.os.replace", side_effect=OSError(28, "No space left")):
            self.assertFalse(config_from_file(self.path, {"REFRESH_TOKEN": "new"}))
        self.assertEqual(config_from_file(self.path), {"REFRESH_TOKEN": "old"})
        self.assertEqual(os.listdir(self.dir.name), ["ecobee.conf"], "temp file left behind")

    def test_bind_mounted_file_falls_back_to_in_place_write(self):
        config_from_file(self.path, {"REFRESH_TOKEN": "old"})
        with mock.patch("pyecobee.util.os.replace", side_effect=OSError(16, "Device or resource busy")):
            self.assertTrue(config_from_file(self.path, {"REFRESH_TOKEN": "new"}))
        self.assertEqual(config_from_file(self.path), {"REFRESH_TOKEN": "new"})
        self.assertEqual(os.listdir(self.dir.name), ["ecobee.conf"])


if __name__ == "__main__":
    unittest.main()
