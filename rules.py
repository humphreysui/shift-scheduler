"""Independent rule checker + per-schedule statistics (no solver dependency).

The checker is intentionally separate from the solver so generated schedules
can be validated with different code than the one that produced them.
"""
from __future__ import annotations

from models import (COMP_AFTER, COVERAGE, NIGHT_RUN_MAX, OFF, SHIFTS,
                    WEIGHT_FLOOR, WEEKDAY_NAMES, WORK_STREAK_MAX, off_pair_label)


def verify(assign, resources, days, day_types, day0=None, day1_overrides=None,
           relax_level=0, prev_work_streak=None, prev_night_streak=None) -> list[dict]:
    """Check a schedule against all structural rules.

    relax_level: number of relaxations applied during generation (0 = full spec).
    Returns a list of violation dicts: {code, severity, day, resource_id, message}.
    """
    violations: list[dict] = []
    day0 = day0 or {}
    day1_overrides = day1_overrides or {}
    prev_work_streak = prev_work_streak or {}
    prev_night_streak = prev_night_streak or {}
    R = list(resources)
    D = len(days)

    def add(code, message, day=None, rid=None, severity="error"):
        violations.append({"code": code, "severity": severity, "day": day,
                           "resource_id": rid, "message": message})

    def value(rid, d):
        seq = assign.get(rid)
        return seq[d] if seq else OFF

    # ---- coverage per day ----
    for d in range(D):
        typ = day_types[d]
        cov = COVERAGE[typ]
        mmin = cov["morning_min"]
        if relax_level >= 4 and typ == "workday":
            mmin = min(mmin, 2)
        s2_max = cov["s2_max"] + (1 if relax_level >= 3 else 0)
        s3_max = cov["s3_max"] + (1 if relax_level >= 3 else 0)
        morning = [r.id for r in R if value(r.id, d) in ("S1", "S4")]
        s1 = [r.id for r in R if value(r.id, d) == "S1"]
        s2 = [r.id for r in R if value(r.id, d) == "S2"]
        s3 = [r.id for r in R if value(r.id, d) == "S3"]
        s4 = [r.id for r in R if value(r.id, d) == "S4"]
        if len(morning) < mmin:
            add("COVER_MORNING", f"Morning crew {len(morning)} (min {mmin})", d)
        if len(morning) > cov["morning_max"]:
            add("COVER_MORNING_MAX", f"Morning crew {len(morning)} (max {cov['morning_max']})", d)
        if len(s2) < cov["s2_min"]:
            add("COVER_S2", "No evening (S2) coverage", d)
        if len(s2) > s2_max:
            add("CAP_S2", f"{len(s2)} people on S2 (max {s2_max})", d)
        if len(s3) < cov["s3_min"]:
            add("COVER_S3", "No night (S3) coverage", d)
        if len(s3) > s3_max:
            add("CAP_S3", f"{len(s3)} people on S3 (max {s3_max})", d)
        if len(s4) > 1:
            add("CAP_S4", f"{len(s4)} people on S4 (max 1)", d)
        if typ == "workday" and not s1:
            add("OFFICE", "Workday without an office person (S1)", d)

    # ---- per resource ----
    for r in R:
        seq = assign.get(r.id, [OFF] * D)
        prev = day0.get(r.id, OFF)
        for d in range(D):
            cur = seq[d]
            if prev == "S3" and cur not in (OFF, "S3"):
                add("TRANSITION", f"Works {cur} right after a night shift (S3)", d, r.id)
            if relax_level < 6 and prev == "S2" and cur in ("S1", "S4"):
                add("REST_EVE", "Starts S1/S4 the day after an S2 shift (~9h rest)", d, r.id)
            prev = cur

        streak = prev_work_streak.get(r.id, 0)
        limit = WORK_STREAK_MAX + (1 if relax_level >= 2 else 0)
        for d in range(D):
            streak = streak + 1 if seq[d] != OFF else 0
            if streak > limit:
                add("WORK_STREAK", f"{streak} consecutive workdays (max {WORK_STREAK_MAX})", d, r.id)

        nstreak = prev_night_streak.get(r.id, 0)
        nlimit = NIGHT_RUN_MAX + (1 if relax_level >= 2 else 0)
        for d in range(D):
            nstreak = nstreak + 1 if seq[d] == "S3" else 0
            if nstreak > nlimit:
                add("NIGHT_STREAK", f"{nstreak} consecutive night shifts (max {NIGHT_RUN_MAX})", d, r.id)

        if relax_level < 5:
            for d in range(D):
                wd = days[d].weekday()
                if wd in r.off_days:
                    if d == 0 and r.id in day1_overrides and day1_overrides[r.id] != OFF:
                        continue  # user override on day 1 wins over the anchor
                    if seq[d] != OFF:
                        add("ANCHOR", f"Works on mandatory off day ({WEEKDAY_NAMES[wd]})", d, r.id)

        if relax_level < 1:
            for s in ("S1", "S2", "S3"):
                if s not in seq:
                    add("ROTATION", f"No {s} within the cycle", None, r.id)

        if r.id in day1_overrides and seq[0] != day1_overrides[r.id]:
            add("DAY1_LOCK", f"Day 1 should be {day1_overrides[r.id]}, found {seq[0]}", 0, r.id)

    # ---- weekday morning weight floor (warning-level) ----
    for d in range(D):
        if day_types[d] == "workday":
            w = sum(rr.weight for rr in R if value(rr.id, d) in ("S1", "S4"))
            if w < WEIGHT_FLOOR:
                add("WEIGHT_BELOW_7", f"Morning weight {w} (floor {WEIGHT_FLOOR})", d, severity="warning")
    return violations


def compute_stats(assign, resources, days, day_types) -> dict:
    R = list(resources)
    D = len(days)
    per = []
    nights, eves = {}, {}
    for r in R:
        seq = assign.get(r.id, [OFF] * D)
        counts = {s: seq.count(s) for s in SHIFTS}
        offs = [d for d in range(D) if seq[d] == OFF]
        extra_rest = [d for d in offs if days[d].weekday() not in r.off_days]
        # Compensatory rest: extra off day (beyond the anchor pair) directly
        # after a run of >= COMP_AFTER consecutive nights.
        comp = []
        for d in range(2, D):
            if seq[d] != OFF or days[d].weekday() in r.off_days:
                continue
            k, i = 0, d - 1
            while i >= 0 and seq[i] == "S3":
                k += 1
                i -= 1
            if k >= COMP_AFTER:
                comp.append(d)
        run = best = 0
        for d in range(D):
            run = run + 1 if seq[d] == "S3" else 0
            best = max(best, run)
        weeks = (D + 6) // 7
        week_work = [sum(1 for dd in range(w * 7, min(w * 7 + 7, D)) if seq[dd] != OFF) for w in range(weeks)]
        per.append({
            "id": r.id, "name": r.name, "weight": r.weight,
            "off_pair": off_pair_label(r.off_pair_start),
            "counts": counts, "off_days": len(offs), "work_days": D - len(offs),
            "extra_rest": [{"day": d, "date": days[d].isoformat()} for d in extra_rest],
            "comp_days": [{"day": d, "date": days[d].isoformat()} for d in comp],
            "comp_count": len(comp),
            "longest_night_run": best,
            "week_work_days": week_work,
            "rotation_ok": all(counts[s] >= 1 for s in ("S1", "S2", "S3")),
        })
        nights[r.id] = counts["S3"]
        eves[r.id] = counts["S2"]
    nv, ev = list(nights.values()), list(eves.values())
    compv = [p["comp_count"] for p in per]
    s4v = [p["counts"]["S4"] for p in per]
    morning = []
    for d in range(D):
        crew = [rr for rr in R if assign.get(rr.id, [OFF] * D)[d] in ("S1", "S4")]
        morning.append({
            "day": d, "date": days[d].isoformat(), "type": day_types[d],
            "weight": sum(rr.weight for rr in crew), "count": len(crew),
        })
    return {
        "resources": per,
        "nights_total": sum(nv),
        "nights_spread": (max(nv) - min(nv)) if nv else 0,
        "eves_total": sum(ev),
        "eves_spread": (max(ev) - min(ev)) if ev else 0,
        "comp_total": sum(compv),
        "comp_spread": (max(compv) - min(compv)) if compv else 0,
        "s4_total": sum(s4v),
        "s4_spread": (max(s4v) - min(s4v)) if s4v else 0,
        "morning": morning,
    }


def streaks_from_tail(tail: dict[str, list[str]]) -> tuple[dict, dict]:
    """Consecutive work / night streak ending at the last value of each tail."""
    work, night = {}, {}
    for rid, vals in tail.items():
        kw = 0
        for v in reversed(vals):
            if v != OFF:
                kw += 1
            else:
                break
        work[rid] = kw
        kn = 0
        for v in reversed(vals):
            if v == "S3":
                kn += 1
            else:
                break
        night[rid] = kn
    return work, night
