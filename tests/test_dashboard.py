"""Tests for app/ecobee_dashboard.py: the data API and the HTTP server."""

from __future__ import annotations

import gzip
import http.client
import json
import os
import threading
import time
import unittest
from datetime import timedelta
from http.server import ThreadingHTTPServer

from helpers import NOW, TempDB

import ecobee_dashboard as dash

A, B, C = "100000000001", "100000000002", "100000000003"


class BucketSeconds(unittest.TestCase):
    def test_short_ranges_are_not_grouped(self):
        for hours in (1, 6, 24, 48):
            self.assertEqual(dash.bucket_seconds(hours * 3600), 0, hours)

    def test_long_ranges_use_round_buckets_and_stay_bounded(self):
        self.assertEqual(dash.bucket_seconds(7 * 86400), 900)
        self.assertEqual(dash.bucket_seconds(30 * 86400), 3600)
        for days in (7, 30, 90, 365, 3650, 36500):
            b = dash.bucket_seconds(days * 86400)
            self.assertLessEqual(days * 86400 / b, dash.MAX_POINTS, days)


class ParseHours(unittest.TestCase):
    def test_values(self):
        self.assertEqual(dash.parse_hours(""), 24)
        self.assertEqual(dash.parse_hours("hours=6"), 6)
        self.assertEqual(dash.parse_hours("hours=0"), 0)
        self.assertEqual(dash.parse_hours("hours=-5"), 0)
        self.assertEqual(dash.parse_hours("hours=abc"), 24)
        self.assertEqual(dash.parse_hours("hours=1e3"), 24)
        self.assertEqual(dash.parse_hours("hours=999999999999"), dash.MAX_HOURS)
        self.assertEqual(dash.parse_hours("hours=" + "9" * 5000), 24)


class BuildPayload(unittest.TestCase):
    def setUp(self):
        self.t = TempDB()

    def tearDown(self):
        self.t.close()

    def payload(self, hours=24):
        self.t.commit()
        return dash.build_payload(self.t.path, hours, now=NOW)

    def test_shape_masking_and_names(self):
        self.t.series(A, "My ecobee", NOW - timedelta(hours=2), 24)
        self.t.series(B, "Main Floor", NOW - timedelta(hours=2), 24)
        self.t.pref(A, display_name="Lake House")
        p = self.payload()
        self.assertEqual(p["bucket_s"], 0)
        self.assertEqual(p["readings"], 48)
        names = [t["display_name"] for t in p["thermostats"]]
        self.assertEqual(sorted(names), ["Lake House", "Main Floor"])
        body = json.dumps(p)
        self.assertNotIn(A, body, "a raw serial number leaked into the API")
        self.assertNotIn(B, body)
        lake = next(t for t in p["thermostats"] if t["display_name"] == "Lake House")
        self.assertEqual(lake["name"], "My ecobee")
        self.assertEqual(lake["identifier"], dash.mask_identifier("testsalt", A))
        self.assertEqual(len(lake["ts"]), 24)
        for col in dash.READING_COLUMNS:
            self.assertEqual(len(lake["series"][col]), 24, col)

    def test_sort_order_then_name(self):
        for ident, name in ((A, "Zeta"), (B, "Alpha"), (C, "Mid")):
            self.t.reading(NOW - timedelta(minutes=5), ident, name)
        self.t.pref(A, sort_order=1)
        names = [t["display_name"] for t in self.payload()["thermostats"]]
        self.assertEqual(names, ["Zeta", "Alpha", "Mid"])

    def test_equipment_is_zero_one_when_not_grouped(self):
        start = NOW - timedelta(minutes=30)
        for i, eq in enumerate(["", "fan", "compCool1,fan", "fan", ""]):
            self.t.reading(start + timedelta(minutes=5 * i), A, equipment=eq)
        t = self.payload()["thermostats"][0]
        self.assertEqual(list(t["equipment"]), ["compCool1", "fan"])  # display order
        self.assertEqual(t["equipment"]["fan"], [0, 1, 1, 1, 0])
        self.assertEqual(t["equipment"]["compCool1"], [0, 0, 1, 0, 0])

    def test_long_range_is_bucketed_and_bounded(self):
        start = NOW - timedelta(days=30)
        # 30 days at 5 minutes: 8640 readings for one thermostat.
        for i in range(30 * 288):
            when = start + timedelta(minutes=5 * i)
            self.t.reading(when, A, temp=60.0 + (i % 12), heat=60.0 + (i % 12),
                           equipment="fan" if i % 4 == 0 else "")
        p = self.payload(hours=720)
        t = p["thermostats"][0]
        self.assertEqual(p["bucket_s"], 3600)
        self.assertLessEqual(len(t["ts"]), dash.MAX_POINTS)
        self.assertEqual(p["readings"], 30 * 288)
        # Readings are aligned to the hour, so each full bucket holds the
        # 12 temps 60..71: the average is 65.5 and the setpoint in force at
        # the end of the hour is the last one, 71.
        full = [i for i in range(len(t["ts"])) if t["series"]["actual_temp_f"][i] == 65.5]
        self.assertGreater(len(full), 700)
        i = full[0]
        self.assertEqual(t["series"]["desired_heat_f"][i], 71.0)
        self.assertEqual(t["equipment"]["fan"][i], 0.25)  # 3 of 12 readings

    def test_all_history_is_bounded(self):
        for day in range(0, 400, 2):
            self.t.reading(NOW - timedelta(days=day), A)
        p = self.payload(hours=0)
        self.assertGreater(p["bucket_s"], 0)
        self.assertLessEqual(len(p["thermostats"][0]["ts"]), dash.MAX_POINTS)

    def test_unit_without_recent_data_still_listed_with_latest(self):
        self.t.reading(NOW - timedelta(minutes=5), A, "Fresh")
        self.t.reading(NOW - timedelta(days=3), B, "Offline", temp=64.0, connected=0)
        p = self.payload(hours=6)
        offline = next(t for t in p["thermostats"] if t["name"] == "Offline")
        self.assertEqual(offline["ts"], [])
        self.assertEqual(offline["latest"]["actual_temp_f"], 64.0)
        self.assertFalse(offline["latest"]["connected"])

    def test_latest_is_the_newest_reading(self):
        self.t.reading(NOW - timedelta(minutes=10), A, "Old name", temp=70.0)
        self.t.reading(NOW - timedelta(minutes=5), A, "New name", temp=71.5, equipment="fan")
        t = self.payload()["thermostats"][0]
        self.assertEqual(t["name"], "New name")
        self.assertEqual(t["latest"]["actual_temp_f"], 71.5)
        self.assertEqual(t["latest"]["equipment_status"], "fan")

    def test_sensors_are_aligned_and_thermostat_sensor_excluded(self):
        start = NOW - timedelta(minutes=15)
        for i in range(3):
            when = start + timedelta(minutes=5 * i)
            self.t.reading(when, A)
            if i != 1:
                self.t.sensor(when, A, "Kitchen", 71.0 + i)
            self.t.sensor(when, A, "Unit itself", 70.0, sensor_type="thermostat")
            self.t.sensor(when, A, "Old sensor", 69.0, sensor_type=None)
        sensors = {s["name"]: s["temperature_f"] for s in self.payload()["thermostats"][0]["sensors"]}
        self.assertEqual(sensors["Kitchen"], [71.0, None, 73.0])
        self.assertIn("Old sensor", sensors)
        self.assertNotIn("Unit itself", sensors)

    def test_a_malformed_timestamp_is_skipped(self):
        self.t.reading(NOW - timedelta(minutes=10), A)
        self.t.reading(NOW - timedelta(minutes=5), A)
        self.t.db.execute(
            "UPDATE thermostat_readings SET ts_utc = '2026-09-01Tgarbage' WHERE id = 1")
        t = self.payload()["thermostats"][0]
        self.assertEqual(len(t["ts"]), 1)

    def test_nulls_survive(self):
        self.t.reading(NOW - timedelta(minutes=5), A, temp=None, humidity=None, out_temp=None)
        t = self.payload()["thermostats"][0]
        self.assertEqual(t["series"]["actual_temp_f"], [None])
        self.assertIsNone(t["latest"]["actual_temp_f"])

    def test_missing_salt_still_masks(self):
        t = TempDB(salt=None)
        try:
            t.reading(NOW - timedelta(minutes=5), A)
            t.commit()
            ident = dash.build_payload(t.path, 24, now=NOW)["thermostats"][0]["identifier"]
            unsalted = dash.mask_identifier("", A)
            self.assertNotEqual(ident, unsalted, "serial hashed without a salt is brute-forceable")
        finally:
            t.close()

    def test_empty_database_without_schema(self):
        t = TempDB(schema=False)
        try:
            p = dash.build_payload(t.path, 24, now=NOW)
            self.assertEqual(p["thermostats"], [])
        finally:
            t.close()

    def test_queries_seek_the_index_instead_of_scanning(self):
        traced = []
        real_connect = dash._connect_readonly

        def connect(path):
            db = real_connect(path)
            db.set_trace_callback(traced.append)
            return db

        self.t.series(A, "U", NOW - timedelta(hours=1), 12)
        self.t.commit()
        dash._connect_readonly = connect
        try:
            dash.build_payload(self.t.path, 6, now=NOW)
            dash.build_payload(self.t.path, 0, now=NOW)
        finally:
            dash._connect_readonly = real_connect
        self.assertGreater(sum("thermostat_readings" in q for q in traced), 5)
        for sql in traced:
            if "thermostat_readings" not in sql or "sqlite_master" in sql:
                continue
            plan = " ".join(r[3] for r in self.t.db.execute("EXPLAIN QUERY PLAN " + sql))
            self.assertNotRegex(plan, r"\bSCAN thermostat_readings\b", sql)

    def test_path_with_special_characters(self):
        t = TempDB()
        try:
            odd = os.path.join(t.dir.name, "a dir?#x")
            os.mkdir(odd)
            path = os.path.join(odd, "ecobee.sqlite3")
            os.replace(t.path, path)
            self.assertEqual(dash.build_payload(path, 24, now=NOW)["thermostats"], [])
        finally:
            t.close()


class ResponseCacheTests(unittest.TestCase):
    def test_bounded_lru(self):
        c = dash.ResponseCache(ttl=60, max_entries=3)
        for k in range(10):
            c.put(k, b"x", b"y")
        self.assertEqual(len(c), 3)
        self.assertIsNone(c.get(0))
        self.assertEqual(c.get(9), (b"x", b"y"))

    def test_expiry(self):
        c = dash.ResponseCache(ttl=0.01, max_entries=3)
        c.put(1, b"x", b"y")
        time.sleep(0.02)
        self.assertIsNone(c.get(1))


class HttpServer(unittest.TestCase):
    """Runs the real handler on an ephemeral port."""

    @classmethod
    def setUpClass(cls):
        cls.t = TempDB()
        now = dash.datetime.now(dash.timezone.utc)
        cls.t.series(A, "My ecobee", now - timedelta(hours=2), 24, equipment="fan")
        cls.t.commit()
        cls.handler = type("H", (dash.Handler,), {"db_path": cls.t.path})
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), cls.handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.t.close()

    def setUp(self):
        dash.CACHE.clear()

    def request(self, path, method="GET", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp, body

    def test_page_has_security_headers_and_no_inline_script(self):
        resp, body = self.request("/")
        self.assertEqual(resp.status, 200)
        csp = resp.getheader("Content-Security-Policy")
        self.assertIn("script-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
        self.assertNotIn("Python", resp.getheader("Server"))
        html = body.decode()
        # The CSP forbids inline code, so the page must not contain any.
        self.assertNotRegex(html, r"<script>(?!\s*</script>)")
        self.assertNotIn("<style>", html)
        self.assertNotRegex(html, r"\son[a-z]+\s*=")  # no onclick= etc.

    def test_static_assets(self):
        for name, ctype in (("theme.css", "text/css"), ("dashboard.css", "text/css"),
                            ("dashboard.js", "text/javascript"), ("dashboard-core.js", "text/javascript")):
            resp, body = self.request(f"/static/{name}")
            self.assertEqual(resp.status, 200, name)
            self.assertTrue(resp.getheader("Content-Type").startswith(ctype), name)
            self.assertGreater(len(body), 100, name)

    def test_static_cannot_escape_its_directory(self):
        for path in ("/static/../ecobee_dashboard.py", "/static/%2e%2e/schema.py",
                     "/static/..%2fschema.py", "/static/", "/static/nope.css",
                     "/static/../../README.md"):
            resp, _ = self.request(path)
            self.assertEqual(resp.status, 404, path)

    def test_etag_revalidation(self):
        resp, _ = self.request("/static/dashboard.js")
        etag = resp.getheader("ETag")
        self.assertTrue(etag)
        resp, body = self.request("/static/dashboard.js", headers={"If-None-Match": etag})
        self.assertEqual(resp.status, 304)
        self.assertEqual(body, b"")

    def test_api_json_and_gzip(self):
        resp, body = self.request("/api/data?hours=24")
        self.assertEqual(resp.status, 200)
        data = json.loads(body)
        self.assertEqual(len(data["thermostats"]), 1)
        self.assertNotIn(A.encode(), body)
        resp, gz = self.request("/api/data?hours=24", headers={"Accept-Encoding": "gzip, br"})
        self.assertEqual(resp.getheader("Content-Encoding"), "gzip")
        self.assertEqual(gzip.decompress(gz), body)
        self.assertLess(len(gz), len(body))

    def test_head(self):
        resp, body = self.request("/api/data", method="HEAD")
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, b"")
        self.assertGreater(int(resp.getheader("Content-Length")), 0)

    def test_hostile_hours_values(self):
        for q in ("hours=999999999999", "hours=-1", "hours=abc", "hours=" + "9" * 5000, "hours=1&hours=2"):
            resp, body = self.request(f"/api/data?{q}")
            self.assertEqual(resp.status, 200, q)
            json.loads(body)

    def test_varying_hours_cannot_grow_the_cache(self):
        for h in range(1, 60):
            self.request(f"/api/data?hours={h}")
        self.assertLessEqual(len(dash.CACHE), dash.CACHE.max_entries)

    def test_unknown_path(self):
        resp, _ = self.request("/api/secret")
        self.assertEqual(resp.status, 404)

    def test_database_error_is_generic(self):
        broken = type("H", (dash.Handler,), {"db_path": "/nonexistent/dir/db.sqlite3"})
        server = ThreadingHTTPServer(("127.0.0.1", 0), broken)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            conn.request("GET", "/api/data?hours=7")
            resp = conn.getresponse()
            body = resp.read().decode()
            self.assertEqual(resp.status, 500)
            self.assertNotIn("nonexistent", body)
            self.assertNotIn("Traceback", body)
        finally:
            server.shutdown()
            server.server_close()


class Performance(unittest.TestCase):
    """A year of 5-minute readings for three thermostats (~315k rows)."""

    @classmethod
    def setUpClass(cls):
        cls.t = TempDB()
        start = NOW - timedelta(days=365)
        rows = []
        for ident in (A, B, C):
            for i in range(365 * 288):
                when = (start + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:%SZ")
                rows.append((when, ident, "U", 1, "cool", "home", "fan" if i % 3 else "",
                             70.0, 45.0, 68.0, 75.0, 35.0, 60.0, 80.0, 50.0))
        cls.t.db.executemany(
            """INSERT INTO thermostat_readings (ts_utc, identifier, name, connected, hvac_mode,
                   current_climate, equipment_status, actual_temp_f, actual_humidity,
                   desired_heat_f, desired_cool_f, desired_humidity, desired_dehumidity,
                   outdoor_temp_f, outdoor_humidity) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        cls.t.commit()

    @classmethod
    def tearDownClass(cls):
        cls.t.close()

    def test_every_range_is_fast_and_small(self):
        for hours in (6, 24, 48, 168, 720, 0):
            t0 = time.perf_counter()
            p = dash.build_payload(self.t.path, hours, now=NOW)
            elapsed = time.perf_counter() - t0
            size = len(json.dumps(p, separators=(",", ":")))
            print(f"\n  hours={hours:<4} {elapsed * 1000:6.0f} ms  {size / 1024:6.0f} KiB", end="")
            for t in p["thermostats"]:
                self.assertLessEqual(len(t["ts"]), dash.MAX_POINTS)
            self.assertLess(size, 600_000, hours)
            self.assertLess(elapsed, 5.0, hours)  # generous: CI machines vary


if __name__ == "__main__":
    unittest.main()
