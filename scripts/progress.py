"""Build progress snapshot.

The workflow journal keys entries by a CONTENT HASH, not a workstream name, so results
cannot be attributed to an area from the journal alone. Count them, and read per-area
activity off the filesystem instead. (Two earlier versions of this script guessed at a
`label` / `key` name field and silently reported 0/4 forever.)
"""
import json, pathlib, sys

AREAS = [("kfs + fixtures", ["kfs", "fixtures"]),
         ("livekit agent",  ["agent"]),
         ("eval harness",   ["evals"]),
         ("web + api",      ["api", "web"])]
TOTAL = 4

journal = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else None
results = 0
if journal and journal.exists():
    for line in journal.read_text().splitlines():
        try:
            if json.loads(line).get("type") == "result": results += 1
        except Exception: pass

def files(dirs):
    return sum(1 for d in dirs for f in pathlib.Path(d).rglob("*")
               if pathlib.Path(d).exists() and f.is_file()
               and f.suffix in {".py", ".json", ".html", ".css", ".md"}
               and "__pycache__" not in str(f))

print()
for label, dirs in AREAS:
    n = files(dirs)
    print(f"  {label:<16} {'█'*min(n,24):<24} {n:>2} files")
print(f"\n  finished  [{'█'*(results*6)}{'░'*max(0,(TOTAL-results)*6)}]  {results}/{TOTAL}\n")
