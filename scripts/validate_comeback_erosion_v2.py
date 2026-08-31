"""
Validation (read-only): re-run the new comeback_erosion (Model C,
regulation-only, close-game credit trigger -- see src/scoring.py's
2026-08-31 redesign) against the benchmark games this metric has been
tuned/validated against across its whole history
(plans/algorithm/watchability_algorithm_open_items.md), before wiring the
new design into the production METRICS registry or touching the test
suite.

Usage:
    venv/bin/python3 scripts/validate_comeback_erosion_v2.py [path/to/cfb.db]
"""
import sys

sys.path.insert(0, ".")

from src import db, espn, scoring

BENCHMARKS = [
    ("401757228", "Marshall/Missouri St", "HIGH (real comeback -- Missouri St scored the winner in the final 2:12)"),
    ("401636872", "KSU/BYU",              "LOW (BYU never seriously threatened, KSU peak coinflip WP only ~0.66)"),
    ("401640993", "LIB/KENN 2024",        "LOW (candidate year for the LT/KENN benchmark)"),
    ("401757313", "LIB/KENN 2025",        "LOW (candidate year for the LT/KENN benchmark)"),
    ("401752665", "ALA/FSU",              "LOW (ALA's high WP was mostly the pregame anchor, not a real earned lead)"),
    ("401760413", "Nevada/SJSU",          "~0 (Nevada pulled away early, never a real threat from SJSU)"),
    ("401628516", "USC/PSU (OT)",         "was HIGH in the old OT-inclusive design -- now regulation-only, so this is the key case to see whether the drama held up in regulation alone"),
    ("401643766", "SDSU/USU",             "HIGH-ish (a real, completed comeback; old design landed ~0.39-0.44)"),
    ("401752816", "BC/MSU (OT)",          "was a FABRICATED 0.444 from corrupted OT rows -- must now be much lower, ideally ~0, since OT is excluded entirely"),
]


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else None
    conn = db.get_connection(db_path)

    print(f"{'game_id':<12}{'label':<24}{'new_erosion':>12}   expectation")
    for game_id, label, expectation in BENCHMARKS:
        row = conn.execute(
            "SELECT home_team_id, watchability_score FROM games WHERE game_id = ?", (game_id,)
        ).fetchone()
        if row is None:
            print(f"{game_id:<12}{label:<24}{'(missing)':>12}   {expectation}")
            continue
        raw = db.get_game_raw_json(conn, game_id)
        if not raw:
            print(f"{game_id:<12}{label:<24}{'(no raw json)':>12}   {expectation}")
            continue
        plays = espn.extract_situational_plays(raw, row["home_team_id"])
        new_score = scoring.comeback_erosion(plays)
        print(f"{game_id:<12}{label:<24}{new_score:>12.4f}   {expectation}")

    print("\n(old watchability_score for reference, mixes comeback_erosion with every other metric so not directly comparable:)")
    for game_id, label, _ in BENCHMARKS:
        row = conn.execute("SELECT watchability_score FROM games WHERE game_id = ?", (game_id,)).fetchone()
        if row:
            print(f"  {game_id} {label}: watchability_score={row['watchability_score']}")


if __name__ == "__main__":
    main()
