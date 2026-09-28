"""Rename and reorder how thermostats appear on the ecobee dashboard.

Preferences are stored in a ``unit_prefs`` table in the same SQLite database
the logger writes to, keyed by the thermostat's immutable ecobee identifier.
The logged data keeps the name reported by ecobee; these settings only change
presentation, so renaming is safe and reversible at any time.

Inside Docker the database path is preset, so these work as-is after
``docker compose exec logger``.

Usage (ecobee's own names are on the left, yours on the right):
    python app/ecobee_units.py list
    python app/ecobee_units.py rename "My ecobee" "Lake House"
    python app/ecobee_units.py order "Lake House" "Main Floor" "Upstairs"
    python app/ecobee_units.py reset "Lake House"   # drop overrides

Names are matched case-insensitively against the identifier, the display
name, or the name reported by ecobee.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

DEFAULT_DB = os.environ.get("ECOBEE_DB", "./data/db/ecobee.sqlite3")

SCHEMA = """
CREATE TABLE IF NOT EXISTS unit_prefs (
    identifier TEXT PRIMARY KEY,
    display_name TEXT,
    sort_order INTEGER
);
"""


def load_units(db: sqlite3.Connection) -> list[dict]:
    """All known thermostats: identifier, ecobee name, display name, sort."""
    rows = db.execute(
        """SELECT identifier, name, MAX(ts_utc)
           FROM thermostat_readings GROUP BY identifier"""
    ).fetchall()
    prefs = {
        r[0]: {"display_name": r[1], "sort_order": r[2]}
        for r in db.execute("SELECT identifier, display_name, sort_order FROM unit_prefs")
    }
    units = []
    for identifier, ecobee_name, _ in rows:
        p = prefs.get(identifier, {})
        units.append(
            {
                "identifier": identifier,
                "ecobee_name": ecobee_name,
                "display_name": p.get("display_name"),
                "sort_order": p.get("sort_order"),
            }
        )
    units.sort(
        key=lambda u: (
            u["sort_order"] is None,
            u["sort_order"] if u["sort_order"] is not None else 0,
            (u["display_name"] or u["ecobee_name"] or "").lower(),
        )
    )
    return units


def resolve(units: list[dict], token: str) -> dict:
    """Find a unit by identifier, display name, or ecobee name."""
    t = token.strip().lower()
    matches = [
        u
        for u in units
        if t in (
            u["identifier"].lower(),
            (u["display_name"] or "").lower(),
            (u["ecobee_name"] or "").lower(),
        )
    ]
    if not matches:
        known = ", ".join(f'"{u["display_name"] or u["ecobee_name"]}"' for u in units)
        sys.exit(f'No thermostat matches "{token}". Known units: {known}')
    if len(matches) > 1:
        sys.exit(f'"{token}" is ambiguous; use the identifier instead.')
    return matches[0]


def cmd_list(db: sqlite3.Connection, _args) -> None:
    units = load_units(db)
    if not units:
        print("No thermostats found — has ecobee_logger.py recorded any readings?")
        return
    w = max(len(u["display_name"] or u["ecobee_name"] or "?") for u in units)
    print(f'{"#":>2}  {"shown as":<{w}}  {"ecobee name":<20} identifier')
    for i, u in enumerate(units, 1):
        shown = u["display_name"] or u["ecobee_name"] or "?"
        pinned = "" if u["sort_order"] is None else " (pinned)"
        print(f'{i:>2}  {shown:<{w}}  {u["ecobee_name"] or "?":<20} {u["identifier"]}{pinned}')


def cmd_rename(db: sqlite3.Connection, args) -> None:
    if not args.new.strip():
        sys.exit('The new name is empty. To go back to ecobee\'s name, use "reset".')
    unit = resolve(load_units(db), args.current)
    db.execute(
        """INSERT INTO unit_prefs (identifier, display_name) VALUES (?, ?)
           ON CONFLICT(identifier) DO UPDATE SET display_name = excluded.display_name""",
        (unit["identifier"], args.new.strip()),
    )
    db.commit()
    print(f'"{unit["display_name"] or unit["ecobee_name"]}" will now be shown as "{args.new.strip()}".')


def cmd_order(db: sqlite3.Connection, args) -> None:
    units = load_units(db)
    resolved = [resolve(units, name) for name in args.names]
    seen = set()
    for u in resolved:
        if u["identifier"] in seen:
            sys.exit(f'"{u["display_name"] or u["ecobee_name"]}" listed twice.')
        seen.add(u["identifier"])
    for position, u in enumerate(resolved, 1):
        db.execute(
            """INSERT INTO unit_prefs (identifier, sort_order) VALUES (?, ?)
               ON CONFLICT(identifier) DO UPDATE SET sort_order = excluded.sort_order""",
            (u["identifier"], position),
        )
    db.commit()
    print("New order (unlisted units follow alphabetically):")
    cmd_list(db, None)


def cmd_reset(db: sqlite3.Connection, args) -> None:
    unit = resolve(load_units(db), args.name)
    db.execute("DELETE FROM unit_prefs WHERE identifier = ?", (unit["identifier"],))
    db.commit()
    print(f'Cleared overrides; unit shows as "{unit["ecobee_name"]}" again.')


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite database (default: {DEFAULT_DB})")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="show units, display names, and order")
    p = sub.add_parser("rename", help="set the display name for a unit")
    p.add_argument("current", help="current name (or identifier)")
    p.add_argument("new", help="new display name")
    p = sub.add_parser("order", help="pin units in the given order")
    p.add_argument("names", nargs="+", help="unit names, first shown first")
    p = sub.add_parser("reset", help="remove display-name/order overrides for a unit")
    p.add_argument("name", help="unit name (or identifier)")

    args = parser.parse_args()
    db_path = os.path.expanduser(args.db)
    if not os.path.isfile(db_path):
        print(f"Database {db_path} not found — run ecobee_logger.py first.", file=sys.stderr)
        return 2
    db = sqlite3.connect(db_path)
    try:
        db.execute("PRAGMA busy_timeout = 5000")  # the logger may be mid-write
        has_readings = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'thermostat_readings'"
        ).fetchone()
        if not has_readings:
            print("No readings yet — start the logger and wait for its first poll.",
                  file=sys.stderr)
            return 2
        db.executescript(SCHEMA)
        {"list": cmd_list, "rename": cmd_rename, "order": cmd_order, "reset": cmd_reset}[
            args.command
        ](db, args)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
