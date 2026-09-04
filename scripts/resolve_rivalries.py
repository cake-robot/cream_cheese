import sqlite3
import sys

sys.path.insert(0, ".")
from scripts._rivalry_raw import RAW

ALIASES = {
    "Miami (FL)": "Miami",
    "UMass": "Massachusetts",
    "Louisiana-Monroe": "UL Monroe",
    "FIU": "Florida International",
    "Hawaii": "Hawai'i",
    "Appalachian State": "App State",
}

# Historic rivalries whose opponent no longer fields an FBS program we track
# (Boston University, University of the Pacific both dropped football;
# Saint Mary's CA rivalry with Oregon predates 1940) -- excluded rather than
# force-matched, since they'll never appear in a discovered game anyway.
SKIP = {"Green Line Rivalry", "Pacific-San José State", "Governors' Trophy Game"}

conn = sqlite3.connect("/Users/ericsayre/Documents/cream_cheese/data/cfb.db")
schools = {
    row[0].lower(): row[0]
    for row in conn.execute("SELECT school FROM teams WHERE school IS NOT NULL")
}

unresolved = set()
resolved = []
for name, a, b in RAW:
    if name in SKIP:
        continue
    ra = ALIASES.get(a, a)
    rb = ALIASES.get(b, b)
    ok_a = ra.lower() in schools
    ok_b = rb.lower() in schools
    if not ok_a:
        unresolved.add(a)
    if not ok_b:
        unresolved.add(b)
    if ok_a and ok_b:
        resolved.append((name, schools[ra.lower()], schools[rb.lower()]))

print(f"resolved: {len(resolved)} / {len(RAW) - len(SKIP)}")
print(f"unresolved schools: {sorted(unresolved)}")

id_by_school = {
    row[1].lower(): row[0]
    for row in conn.execute("SELECT team_id, school FROM teams WHERE school IS NOT NULL")
}

print()
print("PAIRS = [")
for name, a, b in sorted(resolved, key=lambda t: t[0]):
    id_a = id_by_school[a.lower()]
    id_b = id_by_school[b.lower()]
    print(f'    ("{id_a}", "{id_b}", "{name}"),  # {a} vs {b}')
print("]")
