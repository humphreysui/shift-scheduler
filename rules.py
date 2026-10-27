"""Independent rule checker + per-schedule statistics (no solver dependency).

The checker is intentionally separate from the solver so generated schedules
can be validated with different code than the one that produced them.
"""
from __future__ import annotations

from datetime import timedelta

from models import (COMP_AFTER, COVERAGE, NIGHT_RUN_MAX, OFF, SHIFTS,
                    WEIGHT_FLOOR, WEEKDAY_NAMES, WORK_STREAK_MAX, off_pair_label)


def verify(assign, resources, days, day_types, day0=None, day1_overrides=None,
           relax_level=0, prev_work_streak=None, prev_night_streak=None,
           prev_tail=None) -> list[dict]:
    """Check a schedule against all structural rules.

    relax_level: number of relaxations applied during generation (0 = full spec).
    prev_tail: rid -> up to 6 assignment values before the cycle (cross-cycle
        recovery checks: post-night return on day 1, comp day pinned here).
    Returns a list of violation dicts: {code, severity, day, resource_id, message}.
    """
    violations: list[dict] = []
    day0 = day0 or {}
    day1_overrides = day1_overrides or {}
    prev_work_streak = prev_work_streak or {}
    prev_night_streak = prev_night_streak or {}
    prev_tail = prev_tail or {}
    R = list(resources)
    D = len(days)

    def add(code, message, day=None, rid=None, severity="error"):
        v = {"code": code, "severity": severity, "day": day,
             "resource_id": rid, "message": message}
        if v not in violations:            # identical (code, day, rid) reported once
            violations.append(v)

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

        # ---- recovery hard rule 1: post-night return day order ----
        # After any night run + Off recovery run, the first workday back is
        # S2 > S1 > S4 (S4 only when bent at the last relaxation level); a
        # straight S3 back to nights is never in the order. Mirrors the
        # solver's runend recurrence, incl. the day-0 boundary (prev_tail).
        sev_ret = "error" if relax_level < 6 else "warning"
        tl = list(prev_tail.get(r.id, []))

        def night_ok(j):
            if j >= 0:
                return seq[j] == "S3"
            if j == -1 and not tl:
                return day0.get(r.id, OFF) == "S3"
            return j + len(tl) >= 0 and tl[j + len(tl)] == "S3"

        runend = False                      # runend[t] for t < 0 (const prefix)
        for t in range(-len(tl), 0):
            off_t = tl[t + len(tl)] == OFF
            pn = t - 1 >= -len(tl) and tl[t - 1 + len(tl)] == "S3"
            runend = off_t and (pn or runend)
        for d in range(D):
            if runend and seq[d] != OFF and seq[d] not in ("S2", "S1"):
                add("RETURN_ORDER",
                    f"First workday after night recovery is {seq[d]} - "
                    f"hard order is S2 > S1 > S4", d, r.id, severity=sev_ret)
            off_d = seq[d] == OFF
            pn = day0.get(r.id, OFF) == "S3" if d == 0 else seq[d - 1] == "S3"
            runend = off_d and (pn or runend)

        # ---- recovery hard rule 2: a >= 3-night run buys an extra rest day
        # after the regular pair (error below the last relaxation level, loud
        # warning when bent there). Same trigger positions as the solver:
        # in-cycle recovery days and the day pinned in from the previous
        # cycle's tail.
        sev_c3 = "error" if relax_level < 6 else "warning"

        def comp_msg(d1):
            return (f"3-night run ends on the regular days off, but "
                    f"{WEEKDAY_NAMES[days[d1].weekday()]} is a {seq[d1]} day "
                    f"— no extra rest day after the pair")

        for d in range(D):
            if seq[d] != OFF or days[d].weekday() not in r.off_days:
                continue
            if not (night_ok(d - 1) and night_ok(d - 2) and night_ok(d - 3)):
                continue
            d1 = next((k for k in range(d + 1, min(d + 4, D))
                       if days[k].weekday() not in r.off_days), None)
            if d1 is None:
                continue                       # extra day falls in the next cycle
            if seq[d1] != OFF:
                add("COMP3_NO_EXTRA_REST", comp_msg(d1), d1, r.id,
                    severity=sev_c3)

        # cross-seam: the recovery day lived in the previous cycle, so only the
        # extra day can be checked (and it must be Off) here.
        for q in range(-len(tl), 0):
            if tl[q + len(tl)] != OFF:
                continue
            if any(not (q - 1 - k + len(tl) >= 0
                        and tl[q - 1 - k + len(tl)] == "S3") for k in range(3)):
                continue                       # not a >= 3-night run
            if (days[0] + timedelta(days=q)).weekday() not in r.off_days:
                continue                       # the forced Off itself was the comp day
            d1 = next((k for k in range(q + 1, q + 4)
                       if (days[0] + timedelta(days=k)).weekday()
                       not in r.off_days), None)
            if d1 is None or not 0 <= d1 < D:
                continue                       # owned by a neighbouring cycle
            if seq[d1] != OFF:
                add("COMP3_NO_EXTRA_REST", comp_msg(d1), d1, r.id,
                    severity=sev_c3)

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


def compute_stats(assign, resources, days, day_types, prev_tail=None) -> dict:
    R = list(resources)
    D = len(days)
    prev_tail = prev_tail or {}
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
        # 3-night run whose recovery day IS a regular off day: the comp day is
        # the first non-anchor day after the pair (mirrors the solver's
        # W_COMP3_ANCHOR term — without this the credit would vanish).
        for d in range(3, D):
            if seq[d] != OFF or days[d].weekday() not in r.off_days:
                continue
            if not all(seq[i] == "S3" for i in (d - 1, d - 2, d - 3)):
                continue
            d1 = next((k for k in range(d + 1, min(d + 4, D))
                       if days[k].weekday() not in r.off_days), None)
            if d1 is not None and seq[d1] == OFF:
                comp.append(d1)
        # cross-seam: >= 3-night run at the end of the previous cycle whose
        # extra day was pinned into this one - same read as the solver's
        # boundary case (so the ledger credits it here, where the day lives).
        tl = list(prev_tail.get(r.id, []))
        for q in range(-len(tl), 0):
            if tl[q + len(tl)] != OFF:
                continue
            if any(not (q - 1 - k + len(tl) >= 0
                        and tl[q - 1 - k + len(tl)] == "S3") for k in range(3)):
                continue
            if (days[0] + timedelta(days=q)).weekday() not in r.off_days:
                continue                      # the forced Off itself was the comp day
            d1 = next((k for k in range(q + 1, q + 4)
                       if (days[0] + timedelta(days=k)).weekday()
                       not in r.off_days), None)
            if d1 is not None and 0 <= d1 < D and seq[d1] == OFF \
                    and d1 not in comp:
                comp.append(d1)
        comp.sort()                      # chronological, matches the solver view
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
