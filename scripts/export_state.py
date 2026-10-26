"""Build the static site (GitHub Pages artifact) from the local project.

    python scripts/export_state.py --site build/site

Produces:
    <site>/index.html, app.js, style.css, config.json   (copy of static/)
    <site>/data/state.json       exactly what GET /api/state returns
    <site>/data/schedule.csv     exactly what GET /api/export.csv returns
    <site>/data/project.json     the raw store (reference / debugging)
    <site>/data/last_result.json outcome of the last queued job, if any

The same command runs locally and in .github/workflows, so a local
`python -m http.server 8080 -d build/site` exercises the hosted code path.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import app as appmod  # noqa: E402  (needs BASE on sys.path)
from fastapi import HTTPException  # noqa: E402

ASSETS = ("index.html", "app.js", "style.css", "config.json")
COPIES = ("project.json", "last_result.json")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--site", default=str(BASE / "build" / "site"),
                    help="output directory (recreated from scratch)")
    args = ap.parse_args()
    site = Path(args.site)

    if site.exists():
        shutil.rmtree(site)
    site.mkdir(parents=True, exist_ok=True)
    for name in ASSETS:
        shutil.copy2(BASE / "static" / name, site / name)
    data_dir = site / "data"
    data_dir.mkdir()

    state = appmod.get_state()
    (data_dir / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    try:
        csv_text = appmod.export_csv().body.decode("utf-8")
    except HTTPException:  # nothing generated yet
        csv_text = ""
    (data_dir / "schedule.csv").write_text(csv_text, encoding="utf-8")

    copied = []
    for name in COPIES:
        src = appmod.DATA / name
        if src.exists():
            shutil.copy2(src, data_dir / name)
            copied.append(name)

    state_bytes = (data_dir / "state.json").stat().st_size
    print(f"site: {site}")
    print(f"  state.json     {state_bytes / 1024:.1f} KB "
          f"({'current: ' + state['current']['label'] if state.get('current') else 'no schedule'})")
    print(f"  schedule.csv   {(data_dir / 'schedule.csv').stat().st_size / 1024:.1f} KB")
    print(f"  copied         {', '.join(copied) if copied else '(nothing)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
