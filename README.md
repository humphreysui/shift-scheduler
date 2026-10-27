# Shift Scheduler

A local, single-user web app that generates and maintains a rolling **4-week shift
schedule** for **7–9 resources**, honoring availability weights, 24/7 coverage, rest rules,
off-day stability across cycles, night-shift fairness, and a safe manual-edit /
sick-leave "repair" workflow with version backups.

Full behavior spec: `..\..\.opencode\plan\shift-scheduler-spec.md` (or the copy in the
plan folder of the OpenCode workspace).

## Quick start

```
run.bat
```

or manually:

```
python -m pip install -r requirements.txt
python app.py
```

Then open http://127.0.0.1:8765 (opens automatically).

Run the acceptance tests (no server needed):

```
python tests.py
```

## Shifts

| Shift | Hours | Rule |
|---|---|---|
| S1 Office day | 08:00–17:00 | ≥2 per workday (part of the morning crew) |
| S2 Evening | 14:00–23:00 | ≥1 per day |
| S3 Night | 23:00–08:00 (+1) | ≥1 per day; after a night only Off or another S3 |
| S4 Support | 07:00–16:00 | staffed every day (early handover so S3 can rest); max 1/day; prefers lower availability weights, kept equal over time |

## Core rules (as configured)

- **Weekdays**: morning crew (S1+S4) ≥ 3 people, morning availability-weight target 9, floor 7
  (below 7 is best-effort + flagged; a 4th morning person may be added when labor allows).
- **Weekends & holidays**: light coverage — 1 morning + 1 evening + 1 night minimum.
- Every resource: two consecutive mandatory rest days per week (editable pair),
  max 5 consecutive workdays, nights in runs of max 3.
- Slack policy (7 staff ≈ 4 spare person-days/week): weight fixes first, then a
  2nd night person, then a 2nd evening person, remainder as extra rest
  (compensatory rest after night runs has priority whenever coverage allows).
- **Return after nights** (hard rule): after any night block (≥1 S3) and its Off
  recovery day(s), the first workday back follows the order **S2 > S1 > S4** —
  S2 (evening, home) first, then S1 (office); S4 (07:00) only as a last resort once
  S2 and S1 are impossible, and going straight back to nights (S3) is never part of
  the order. A breach is an error in normal generation; only the last relaxation
  level may bend it, and then the day is flagged red in the UI. The scan walks the
  whole rest run (not just one day) and works across cycle boundaries.
- **Rest after evenings**: after an S2 (ends 23:00) the next day is never S1 or S4
  (07:00–08:00 starts → only 8–9h rest) — a hard rule; bent only as the very last
  resort and the affected day is flagged.
- **Compensatory rest**: an extra rest day right after a run of ≥2 nights (a 3-night
  run is strongly preferred).  Comp days are counted per person, shown as **C** in the
  grid, and equalized across cycles with their own debt ledger entry.

  **Extra rest after a 3‑night run (hard rule)**: every ≥3‑night run buys one
  extra rest day. When the recovery slot IS the resource's fixed pair, the first
  non‑anchor day after the pair is **forced Off** — generation cannot skip it
  (the forced Off right after the run already counts when it falls on a weekday).
  If coverage or a locked cell makes it impossible, the schedule only solves at
  the last relaxation level and the day is flagged red (an error below that level).
  Runs are read across cycle boundaries: a ≥3‑night run at the end of one cycle
  pins the extra day into the start of the next, so nothing goes silent at the seam.
  The day is tracked in the comp debt ledger just like any other compensatory
  rest day, so it carries over and is repaid across cycles.
- **Support shift (S4)**: staffed every day (07:00 handover takes over so the night
  person can leave on time), tilted toward lower availability weights, spread equally across
  the team and tracked in the debt ledger.
- Night fairness: night counts equalized per cycle; the app tracks a **debt ledger**
  so a surplus in one cycle is corrected in the next.
- Off-day equality across cycles: total rest days (anchors + comp days + slack
  extras) carry their own ledger entry, so extra rest received in one cycle is
  repaid later — everyone converges to the same off-day total over the year.
  The ledger accumulates across every stored cycle (nothing is forgotten when a
  new cycle is generated); per-person cumulative totals are shown in the People tab.
- Evening fairness: S2 counts carry their own cumulative ledger entry (same
  mechanism as nights) — a surplus in one cycle is repaid in later cycles.
- Boundary lookahead: the last day of a cycle steers evening/night shifts to
  people who rest on the next cycle's first weekday, so a new generation never
  inherits a Monday that is short on morning staff (which would otherwise force
  a reported relaxation of the 3-person morning minimum).
- Rotation: every resource works S1, S2 and S3 at least once per 4-week cycle
  (the order is flexible — the rest rules still apply; e.g. an S2 is never
  followed by an S1/S4).
- Sick leave: click any cell → set Off / other shift → the rest of the cycle is
  re-solved with minimal changes to others; changes are listed, the previous
  version is kept in History and can be restored. If you restore an older
  version, the next cycle's boundary follows the restored version (the same
  source of truth as the ledger) — superseded repair drafts no longer leak
  into new generations.
- **Rule conflict switch** (Setup → priority): at 7 staff, holding the morning
  weight floor on *every* weekday and perfect night equality conflict. Default
  (spec order): weight floor first → (nearly) no below-7 days — a very tight
  roster can force a single flagged one under the evening-rest rule — night
  counts spread ≤2, and the debt ledger corrects the surplus next cycle.
  Alternatively "Night equality first" → perfect night balance, a couple of
  below-7 mornings are flagged for manual edit. Flip it in Setup → Save settings.
- If rules must be bent (e.g. under-staffed day), the app relaxes them in a fixed
  order and reports exactly what was relaxed — nothing is broken silently.

## Hosted version (free, GitHub Pages)

The same UI runs as a static site: **https://humphreysui.github.io/shift-scheduler/**
(source: https://github.com/humphreysui/shift-scheduler, public).

Because a static site has no server, every edit becomes a queued job:

1. You click something that writes (repair a cell, generate, save settings…).
2. The page opens a GitHub issue titled `[job] …` carrying the exact same JSON
   payload the local API would have received.
3. The `Run queued job` Action executes it through the real `app.py` endpoints,
   commits `data/project.json`, comments the result on the issue and closes it,
   then republishes the site.
4. The page polls `data/last_result.json` and refreshes itself — a full
   issue → run → deploy cycle measured **42 s** (jobs run one at a time, so a
   second edit queues behind the first).

Reads (`data/state.json`, the CSV export) come straight from the published
artifact, so browsing is instant. Only writes are queued. History diff
("Changes" buttons) is hidden in hosted mode; everything else — all six ops —
works.

Networking note: on a network where `github.com` is unreachable, git pushes go
through SSH on `ssh.github.com:443` with a per-repo deploy key, and workflow
files go through the REST API. See the "HOSTED MODE" section of `BUILD_RULES.txt`.

## Files

```
app.py         FastAPI server + API
scheduler.py   CP-SAT (OR-Tools) scheduling engine
rules.py       independent rule checker + stats
store.py       JSON persistence (versions, ledger, start state)
models.py      constants (shifts, coverage, rules)
static/        single-page UI (Schedule grid, Day view, People, Setup, History)
tests.py       acceptance tests (AT1–AT5 + ledger carryover)
data/project.json   all user data (auto-created; back it up)
scripts/       export_state.py (build the Pages site), run_job.py (run a queued
               issue), push_via_api.py (REST push fallback)
.github/workflows/  pages.yml (publish), schedule-job.yml (run queued jobs)
```
