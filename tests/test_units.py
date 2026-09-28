"""Tests for app/ecobee_units.py (rename / reorder thermostats), run as a CLI."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from datetime import timedelta

from helpers import NOW, ROOT, TempDB

SCRIPT = os.path.join(ROOT, "app", "ecobee_units.py")


class UnitsCli(unittest.TestCase):
    def setUp(self):
        self.t = TempDB()
        for ident, name in (("100000000001", "My ecobee"), ("100000000002", "Main Floor"),
                            ("100000000003", "Upstairs")):
            self.t.reading(NOW - timedelta(minutes=5), ident, name)
        self.t.commit()

    def tearDown(self):
        self.t.close()

    def run_cli(self, *args, db=None):
        return subprocess.run(
            [sys.executable, SCRIPT, "--db", db or self.t.path, *args],
            capture_output=True, text=True, timeout=30,
        )

    def shown(self):
        out = self.run_cli("list").stdout.splitlines()[1:]
        return [line.split("  ")[1].strip() for line in out]

    def test_list(self):
        r = self.run_cli("list")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.shown(), ["Main Floor", "My ecobee", "Upstairs"])

    def test_rename_and_reset(self):
        r = self.run_cli("rename", "my ECOBEE", "Lake House")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Lake House", self.shown())
        self.assertEqual(self.run_cli("rename", "Lake House", "Cabin").returncode, 0)
        self.assertIn("Cabin", self.shown())
        self.assertEqual(self.run_cli("reset", "Cabin").returncode, 0)
        self.assertIn("My ecobee", self.shown())

    def test_rename_rejects_empty_name(self):
        r = self.run_cli("rename", "Upstairs", "   ")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("reset", r.stderr)

    def test_order_pins_listed_units_first(self):
        self.assertEqual(self.run_cli("order", "Upstairs", "My ecobee").returncode, 0)
        self.assertEqual(self.shown(), ["Upstairs", "My ecobee", "Main Floor"])

    def test_order_rejects_duplicates_and_unknown_names(self):
        r = self.run_cli("order", "Upstairs", "upstairs")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("twice", r.stderr)
        r = self.run_cli("order", "Basement")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Known units", r.stderr)

    def test_identifier_disambiguates_duplicate_names(self):
        self.t.reading(NOW, "100000000004", "Upstairs")
        self.t.commit()
        r = self.run_cli("rename", "Upstairs", "X")
        self.assertIn("ambiguous", r.stderr)
        self.assertEqual(self.run_cli("rename", "100000000004", "Attic").returncode, 0)
        self.assertIn("Attic", self.shown())

    def test_missing_database_and_empty_database(self):
        r = self.run_cli("list", db=os.path.join(self.t.dir.name, "nope.sqlite3"))
        self.assertEqual(r.returncode, 2)
        empty = TempDB(schema=False)
        try:
            r = self.run_cli("list", db=empty.path)
            self.assertEqual(r.returncode, 2)
            self.assertIn("No readings yet", r.stderr)
        finally:
            empty.close()


if __name__ == "__main__":
    unittest.main()
