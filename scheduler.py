"""CP-SAT scheduling engine.

Objective tiers (weighted sum, big gaps so it behaves lexicographically):
  1. weekdays below morning weight floor (1e6 each)      - minimize
  2. extra (4th) morning person cost (2000 each)         - minimize
  3. compensatory rest after a 3-night run (2500)        - maximize
  4. weekday mornings toward weight target (200/max 600) - maximize
  5. post-night return day prefers S2, avoids S1/S4      - steer
  6. 2nd night S3 coverage (1000 each)                   - maximize (night priority)
  7. 2nd evening S2 coverage (150 each)                  - maximize
  8. night fairness: cumulative balance incl. ledger  - balance
  9. comp / S4 / off-day fairness: cumulative balance incl. ledger - balance
 10. S4 support shift: daily-presence bonus + tilted assignment - steer
 11. evening fairness: cumulative balance incl. ledger    - balance
 12. repair-mode change penalties (other 50k / self 500)  - minimize
 13. boundary lookahead: last day's S2/S3 prefers people resting on the
     next cycle's first weekday (nothing can strand the next Monday) - steer

Hard transitions: after a night (S3) only Off/S3; after an evening (S2) the
next day may not be S1/S4 (only ~9h rest) — relaxed only as the last resort.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from models import (COVERAGE, CYCLE_DAYS, NIGHT_RUN_MAX, OFF, SHIFTS,
                    WEIGHT_FLOOR, WEIGHT_TARGET, WORK_STREAK_MAX, day_type)
from rules import compute_stats, verify

try:
    from ortools.sat.python import cp_model
except ImportError:  # pragma: no cover
    cp_model = None

W_BELOW_FLOOR = 1_000_000
W_EXTRA_MORNING = 2_000
W_COMP3 = 2_500        # extra rest day right after a 3-night run
W_COMP2 = 800          # extra rest day after a 2-night run
W_COMP3_ANCHOR = 2_500  # 3-night run whose recovery IS the regular pair: pay for
                        # the extra day AFTER the pair (same benefit as W_COMP3 -
                        # without it the -W_COMP3 term is unreachable on anchor
                        # days and the solver designs runs around it instead)
W_S3_COVERAGE = 1_000
W_TOWARD_TARGET = 200
W_RETURN_S2 = 300      # first workday after night recovery: prefer S2 (wfh)
W_RETURN_AVOID = 800   # ... avoid S1 (office) / S4 (07:00) that day
W_S2_COVERAGE = 150
W_NIGHT_CUM_SPREAD = 50              # per hundredth of cumulative night imbalance
W_NIGHT_CUM_ABS = 25                 # ... plus per hundredth away from the common target
# "nights_first" priority (optional setting): swap these two tiers so night
# equality dominates the morning weight floor. At 7 staff the rules conflict:
# floor-first -> 0 below-7 days, night cumulative spread 2 (debt fixes later
# cycles); nights_first -> perfect night equality, a few below-7 days to fix.
W_FLOOR_NIGHTS_FIRST = 5_000
W_NIGHT_CUM_SPREAD_NIGHTS_FIRST = 5_000
# fairness: balance the cumulative ledger (ledger debt + count x 100) via the
# spread of the values plus the total distance from their common target. Both
# are zero at perfect equality; distance gives every incumbent a gradient to
# follow, and neither can reward overshoot past equality (unlike a constant
# per-assignment debt bias, which over-corrected badly once).
W_COMP_CUM_SPREAD = 50
W_COMP_CUM_ABS = 20
# total off days (anchors + comps + slack extras) persist across cycles the
# same way. Tuned on the live 7-staff roster (probe runs on the repaired
# boundary): at 15/6 two resources kept 13 rest days vs 8 (spread 5); 50/25
# still left spread 3; 80/40 settles at spread 2 with a mild morning-weight
# cost (7.4 vs 7.6 avg on workdays). Off-day equality is the top fairness
# priority, so it outranks the within-cycle evening balance.
W_OFF_CUM_SPREAD = 80
W_OFF_CUM_ABS = 40
# evening (S2) fairness upgraded to the same cumulative balance as the other
# resources: 100 x cycle count + ledger debt, balanced via spread + distance.
# The old within-cycle range term plus a weak linear debt bias could not carry
# a surplus across cycles; this form repays deficits in later cycles wherever
# the morning floor allows it (the floor always wins at 1e6).
W_EVE_CUM_SPREAD = 50
W_EVE_CUM_ABS = 25
# boundary lookahead: the day after the cycle is the next cycle's first day.
# An S2/S3 on the last day restricts that person's next day (Off/S3 after a
# night, no S1/S4 after an evening); if it lands on someone who would normally
# work that day it can starve the next cycle's Monday morning minimum, which
# is an input to the next generation and cannot be relaxed. Prefer routing the
# last day's evening/night to people who rest on that weekday anyway.
W_BOUNDARY_LOOKAHEAD = 1200
W_S4_PRESENCE = 1500   # per day with a staffed S4 (07:00 handover lets S3 rest)
W_S4_DAILY = 300       # per S4 assignment (who takes it: tilt + balance below)
W_S4_TILT = 100        # per weight level below 3: tilt S4 to lighter resources
W_S4_CUM_SPREAD = 8
W_S4_CUM_ABS = 6
W_WORK_SPREAD = 100
W_CHANGE_OTHER = 50_000
W_CHANGE_SELF = 500
W_ANCHOR_SOFT = 200_000
M_BELOW = 12

RELAX_LABELS = [
    "Rotation guarantee relaxed: not every resource could work S1+S2+S3 within this cycle.",
    "Consecutive-day limits relaxed (a longer work / night stretch was allowed).",
    "Coverage caps relaxed (extra S2/S3 slots were added to fit the roster).",
    "Morning minimum lowered: a day is below the 3-person morning minimum — review.",
    "Off-day anchors relaxed: someone works on a mandatory rest day — review.",
    "Evening rest relaxed: someone starts S1/S4 the day after an S2 shift (~9h rest) — review.",
]


@dataclass
class SolveParams:
    resources: list
    start_date: date
    light_days: set = field(default_factory=set)
    day0: dict = field(default_factory=dict)            # rid -> "yesterday" value
    day1_overrides: dict = field(default_factory=dict)  # rid -> locked day-1 value
    debt: dict = field(default_factory=dict)            # rid -> night/eve/comp/s4/off (hundredths)
    prev_work_streak: dict = field(default_factory=dict)
    prev_night_streak: dict = field(default_factory=dict)
    baseline: dict | None = None                        # rid -> list of values (repair mode)
    change_from_day: int = 0                            # first day allowed to change = the edited
                                                        # day; earlier days are frozen to baseline
    edited_resource: str | None = None                  # its cell at change_from_day is forced
    priority: str = "floor_first"   # "floor_first" (spec default) | "nights_first"
    # (S4 policy is built-in since v1.2 — no s4_mode parameter)
    time_limit_s: float = 180.0


@dataclass
class SolveResult:
    status: str
    relax_level: int = 0
    relaxations: list = field(default_factory=list)
    assign: dict = field(default_factory=dict)
    days: list = field(default_factory=list)
    day_types: list = field(default_factory=list)
    flags: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def _lsum(expressions):
    return cp_model.LinearExpr.Sum(list(expressions))


def _and(model, lits, name):
    """BoolVar that is true iff every literal in `lits` is true."""
    if len(lits) == 1:
        return lits[0]
    q = model.NewBoolVar(name)
    model.AddBoolOr([l.Not() for l in lits] + [q])
    for l in lits:
        model.AddImplication(q, l)
    return q


def _solve_once(p: SolveParams, relax: int):
    R = [r for r in p.resources if r.active]
    D = CYCLE_DAYS
    days = [p.start_date + timedelta(days=i) for i in range(D)]
    day_types = [day_type(d, p.light_days) for d in days]

    model = cp_model.CpModel()
    x, off = {}, {}
    for r in R:
        for d in range(D):
            for s in SHIFTS:
                x[r.id, d, s] = model.NewBoolVar(f"x_{r.id}_{d}_{s}")
            off[r.id, d] = model.NewBoolVar(f"off_{r.id}_{d}")
            model.Add(sum(x[r.id, d, s] for s in SHIFTS) + off[r.id, d] == 1)

    def val_var(rid, d, v):
        return off[rid, d] if v == OFF else x[rid, d, v]

    # ---- repair: freeze days before change_from_day to the baseline ----
    freeze_until = 0
    if p.baseline is not None:
        freeze_until = min(max(p.change_from_day, 0), D)
        for r in R:
            arr = p.baseline.get(r.id)
            if not arr:
                continue
            for d in range(freeze_until):
                model.Add(val_var(r.id, d, arr[d]) == 1)
        # the edited cell is forced to its baseline value, while the edited
        # day itself stays open to everyone else so required coverage can
        # move onto it (a sick night must be re-covered by someone else —
        # freezing the whole edit day through made single-night days
        # provably infeasible at every relaxation level).
        if p.edited_resource is not None and 0 <= p.change_from_day < D:
            arr = p.baseline.get(p.edited_resource)
            if arr and len(arr) > p.change_from_day:
                model.Add(val_var(p.edited_resource, p.change_from_day,
                                  arr[p.change_from_day]) == 1)

    # ---- transitions: after a night (S3) only Off or another S3 ----
    for r in R:
        if p.day0.get(r.id, OFF) == "S3":
            model.Add(off[r.id, 0] + x[r.id, 0, "S3"] >= 1)
        for d in range(1, D):
            model.Add(off[r.id, d] + x[r.id, d, "S3"] >= x[r.id, d - 1, "S3"])

    # ---- rest rule: after an evening (S2) the next day is not S1/S4 ----
    # S2 ends at 23:00; S1 starts 08:00 / S4 at 07:00 next day (8-9h rest).
    if relax < 6:
        for r in R:
            if p.day0.get(r.id, OFF) == "S2":
                model.Add(x[r.id, 0, "S1"] + x[r.id, 0, "S4"] == 0)
            for d in range(1, D):
                model.Add(x[r.id, d - 1, "S2"] + x[r.id, d, "S1"] + x[r.id, d, "S4"] <= 1)

    # ---- max work streak / max night run (rolling windows with day-0 context) ----
    wstreak_limit = WORK_STREAK_MAX + (1 if relax >= 2 else 0)
    nrun_limit = NIGHT_RUN_MAX + (1 if relax >= 2 else 0)
    for r in R:
        kw = p.prev_work_streak.get(r.id, 0)
        for s in range(-5, D - 5):
            known = sum(1 for o in range(s, s + 6) if o < 0 and -o <= kw)
            wrt = [1 - off[r.id, o] for o in range(max(s, 0), s + 6)]
            model.Add(_lsum(wrt) + known <= wstreak_limit)
        kn = p.prev_night_streak.get(r.id, 0)
        for s in range(-3, D - 3):
            known = sum(1 for o in range(s, s + 4) if o < 0 and -o <= kn)
            nrt = [x[r.id, o, "S3"] for o in range(max(s, 0), s + 4)]
            model.Add(_lsum(nrt) + known <= nrun_limit)

    # ---- off-day anchors ----
    if relax < 5:
        for r in R:
            for d in range(D):
                if d < freeze_until:
                    continue
                if days[d].weekday() in r.off_days:
                    if d == 0 and r.id in p.day1_overrides and p.day1_overrides[r.id] != OFF:
                        continue  # user day-1 override wins over the anchor
                    model.Add(off[r.id, d] == 1)

    # ---- day-1 user overrides ----
    for rid, v in p.day1_overrides.items():
        if v != OFF and any(rr.id == rid for rr in R):
            model.Add(val_var(rid, 0, v) == 1)
        elif v == OFF and any(rr.id == rid for rr in R):
            model.Add(off[rid, 0] == 1)

    # ---- rotation guarantee (cycle level) ----
    if relax < 1:
        for r in R:
            for s in ("S1", "S2", "S3"):
                model.Add(sum(x[r.id, d, s] for d in range(D)) >= 1)

    # ---- coverage ----
    morning_exprs = {}
    for d in range(D):
        typ = day_types[d]
        cov = COVERAGE[typ]
        morning = _lsum([x[r.id, d, "S1"] + x[r.id, d, "S4"] for r in R])
        morning_exprs[d] = morning
        mmin = cov["morning_min"]
        if relax >= 4 and typ == "workday":
            mmin = 2
        model.Add(morning >= mmin)
        model.Add(morning <= cov["morning_max"])
        s2 = _lsum([x[r.id, d, "S2"] for r in R])
        s3 = _lsum([x[r.id, d, "S3"] for r in R])
        s2max = cov["s2_max"] + (1 if relax >= 3 else 0)
        s3max = cov["s3_max"] + (1 if relax >= 3 else 0)
        model.Add(s2 >= cov["s2_min"])
        model.Add(s2 <= s2max)
        model.Add(s3 >= cov["s3_min"])
        model.Add(s3 <= s3max)
        model.Add(sum(x[r.id, d, "S4"] for r in R) <= 1)
        if typ == "workday":
            model.Add(sum(x[r.id, d, "S1"] for r in R) >= 1)

    # ---- objective ----
    terms = []

    for d in range(D):
        typ = day_types[d]
        if typ == "workday":
            mw = _lsum([r.weight * (x[r.id, d, "S1"] + x[r.id, d, "S4"]) for r in R])
            b = model.NewBoolVar(f"b7_{d}")
            model.Add(mw + M_BELOW * b >= WEIGHT_FLOOR)
            terms.append((W_FLOOR_NIGHTS_FIRST if p.priority == "nights_first"
                          else W_BELOW_FLOOR) * b)
            m9 = model.NewIntVar(0, WEIGHT_TARGET, f"m9_{d}")
            model.Add(m9 <= mw)
            terms.append(-W_TOWARD_TARGET * m9)
        max_extra = COVERAGE[typ]["morning_max"] - 3
        if max_extra > 0:
            e = model.NewIntVar(0, max_extra, f"em_{d}")
            model.Add(e >= morning_exprs[d] - 3)
            terms.append(W_EXTRA_MORNING * e)

    s3tot = _lsum([x[r.id, d, "S3"] for r in R for d in range(D)])
    terms.append(-W_S3_COVERAGE * s3tot)
    s2tot = _lsum([x[r.id, d, "S2"] for r in R for d in range(D)])
    terms.append(-W_S2_COVERAGE * s2tot)

    ncum = []
    for r in R:
        n = model.NewIntVar(0, D, f"n_{r.id}")
        model.Add(n == sum(x[r.id, d, "S3"] for d in range(D)))
        base = round(p.debt.get(r.id, {}).get("night", 0))  # hundredths
        c = model.NewIntVar(-1_000_000, 1_000_000, f"ncum_{r.id}")
        model.Add(c == 100 * n + base)
        ncum.append(c)
    mx = model.NewIntVar(-1_000_000, 1_000_000, "nmax")
    mn = model.NewIntVar(-1_000_000, 1_000_000, "nmin")
    for c in ncum:
        model.Add(mx >= c)
        model.Add(mn <= c)
    terms.append((W_NIGHT_CUM_SPREAD_NIGHTS_FIRST if p.priority == "nights_first"
                  else W_NIGHT_CUM_SPREAD) * (mx - mn))
    ntgt = model.NewIntVar(-1_000_000, 1_000_000, "ntgt")
    for i, c in enumerate(ncum):
        dev = model.NewIntVar(0, 2_000_000, f"ndev_{i}")
        model.AddAbsEquality(dev, c - ntgt)
        terms.append(W_NIGHT_CUM_ABS * dev)

    # ---- evening (S2) fairness: cumulative balance incl. ledger ----
    # Same mechanism as nights/comp/S4/off so a surplus in one cycle is repaid
    # in the cycles where the morning floor allows it.
    ecum = []
    for r in R:
        ev = model.NewIntVar(0, D, f"e_{r.id}")
        model.Add(ev == sum(x[r.id, d, "S2"] for d in range(D)))
        base = round(p.debt.get(r.id, {}).get("eve", 0))  # hundredths
        c = model.NewIntVar(-1_000_000, 1_000_000, f"ecum_{r.id}")
        model.Add(c == 100 * ev + base)
        ecum.append(c)
    emx = model.NewIntVar(-1_000_000, 1_000_000, "emax")
    emn = model.NewIntVar(-1_000_000, 1_000_000, "emin")
    for c in ecum:
        model.Add(emx >= c)
        model.Add(emn <= c)
    terms.append(W_EVE_CUM_SPREAD * (emx - emn))
    etgt = model.NewIntVar(-1_000_000, 1_000_000, "etgt")
    for i, c in enumerate(ecum):
        dev = model.NewIntVar(0, 2_000_000, f"edev_{i}")
        model.AddAbsEquality(dev, c - etgt)
        terms.append(W_EVE_CUM_ABS * dev)

    wvars = []
    for r in R:
        wv = model.NewIntVar(0, D, f"w_{r.id}")
        model.Add(wv == D - sum(off[r.id, d] for d in range(D)))
        wvars.append(wv)
    wmx = model.NewIntVar(0, D, "wmax")
    wmn = model.NewIntVar(0, D, "wmin")
    for wv in wvars:
        model.Add(wmx >= wv)
        model.Add(wmn <= wv)
    terms.append(W_WORK_SPREAD * (wmx - wmn))

    if p.baseline is not None:
        for r in R:
            arr = p.baseline.get(r.id)
            if not arr:
                continue
            wgt = W_CHANGE_SELF if r.id == p.edited_resource else W_CHANGE_OTHER
            for d in range(min(p.change_from_day, D), D):
                terms.append(wgt * (1 - val_var(r.id, d, arr[d])))

    if relax >= 5:
        for r in R:
            for d in range(D):
                if days[d].weekday() in r.off_days:
                    terms.append(W_ANCHOR_SOFT * (1 - off[r.id, d]))

    # ---- post-night return preference ----
    # First workday after a night-recovery day (S3, then Off): prefer S2
    # (evening, work from home) over S1 (office) or S4 (07:00 support), so the
    # resource can rest more after the night block.
    for r in R:
        for d in range(1, D):
            if d == 1:
                if p.day0.get(r.id, OFF) != "S3":
                    continue
                pre = off[r.id, 0]
            else:
                pre = _and(model, [x[r.id, d - 2, "S3"], off[r.id, d - 1]],
                           f"ret_{r.id}_{d}")
            q2 = _and(model, [pre, x[r.id, d, "S2"]], f"ret2_{r.id}_{d}")
            terms.append(-W_RETURN_S2 * q2)
            q1 = _and(model, [pre, x[r.id, d, "S1"]], f"ret1_{r.id}_{d}")
            terms.append(W_RETURN_AVOID * q1)
            q4 = _and(model, [pre, x[r.id, d, "S4"]], f"ret4_{r.id}_{d}")
            terms.append(W_RETURN_AVOID * q4)

    # ---- compensatory rest day right after a night run ----
    # An extra Off day (not an anchor day) directly after the last night of a
    # run of >= 2 (>= 3 rewarded much more). Counts are persisted and equalized
    # across cycles via the comp debt ledger.
    comp_exprs = {r.id: [] for r in R}
    for r in R:
        for d in range(2, D):
            if days[d].weekday() in r.off_days:
                continue
            base = [off[r.id, d], x[r.id, d - 1, "S3"], x[r.id, d - 2, "S3"]]
            if d >= 3:
                c3 = _and(model, base + [x[r.id, d - 3, "S3"]], f"c3_{r.id}_{d}")
                terms.append(-W_COMP3 * c3)
                c2 = _and(model, base + [x[r.id, d - 3, "S3"].Not()], f"c2_{r.id}_{d}")
                terms.append(-W_COMP2 * c2)
                comp_exprs[r.id] += [c2, c3]
            else:  # d == 2: the run may have started before the cycle
                c2 = _and(model, base, f"c2_{r.id}_{d}")
                terms.append(-W_COMP2 * c2)
                comp_exprs[r.id].append(c2)

    # ---- anchor-aligned recovery: the rest right after a 3-night run IS the
    # resource's fixed pair, so the -W_COMP3 reward above is unreachable and the
    # solver simply ends runs in front of the pair (the live Nov 1-28 roster has
    # 5 such runs and 0 comp days). Pay for the EXTRA day after the pair instead
    # and count it in the comp ledger, exactly like a non-anchor comp day.
    for r in R:
        for d in range(3, D):
            if days[d].weekday() not in r.off_days:
                continue                      # recovery day must be a regular off day
            d1 = next((k for k in range(d + 1, min(d + 4, D))
                       if days[k].weekday() not in r.off_days), None)
            if d1 is None:
                continue                      # extra day would fall in the next cycle
            e = _and(model, [off[r.id, d], x[r.id, d - 1, "S3"], x[r.id, d - 2, "S3"],
                             x[r.id, d - 3, "S3"], off[r.id, d1]], f"c3a_{r.id}_{d}")
            terms.append(-W_COMP3_ANCHOR * e)
            comp_exprs[r.id].append(e)

    ccum = []
    for r in R:
        cv = model.NewIntVar(0, D, f"c_{r.id}")
        model.Add(cv == _lsum(comp_exprs[r.id]))
        base = round(p.debt.get(r.id, {}).get("comp", 0))  # hundredths
        c = model.NewIntVar(-1_000_000, 1_000_000, f"ccum_{r.id}")
        model.Add(c == 100 * cv + base)
        ccum.append(c)
    cmx = model.NewIntVar(-1_000_000, 1_000_000, "cmax")
    cmn = model.NewIntVar(-1_000_000, 1_000_000, "cmin")
    for c in ccum:
        model.Add(cmx >= c)
        model.Add(cmn <= c)
    terms.append(W_COMP_CUM_SPREAD * (cmx - cmn))
    ctgt = model.NewIntVar(-1_000_000, 1_000_000, "ctgt")
    for i, c in enumerate(ccum):
        dev = model.NewIntVar(0, 2_000_000, f"cdev_{i}")
        model.AddAbsEquality(dev, c - ctgt)
        terms.append(W_COMP_CUM_ABS * dev)

    # ---- total off-day (rest) fairness: cumulative balance incl. ledger ----
    # Comp days are one component of rest; this balances the REST TOTAL
    # (anchor days + comp days + slack extras) so year-over-year everyone
    # lands on the same number of off days, not just the same comps.
    ocum = []
    for r in R:
        ov = model.NewIntVar(0, D, f"offcnt_{r.id}")
        model.Add(ov == sum(off[r.id, d] for d in range(D)))
        base = round(p.debt.get(r.id, {}).get("off", 0))  # hundredths
        c = model.NewIntVar(-1_000_000, 1_000_000, f"ocum_{r.id}")
        model.Add(c == 100 * ov + base)
        ocum.append(c)
    omx = model.NewIntVar(-1_000_000, 1_000_000, "offmax")
    omn = model.NewIntVar(-1_000_000, 1_000_000, "offmin")
    for c in ocum:
        model.Add(omx >= c)
        model.Add(omn <= c)
    terms.append(W_OFF_CUM_SPREAD * (omx - omn))
    otgt = model.NewIntVar(-1_000_000, 1_000_000, "offtgt")
    for i, c in enumerate(ocum):
        dev = model.NewIntVar(0, 2_000_000, f"offdev_{i}")
        model.AddAbsEquality(dev, c - otgt)
        terms.append(W_OFF_CUM_ABS * dev)

    # ---- S4 support shift: staffed daily (early S3 handover) ----
    # A per-day presence bonus keeps the slot filled; the per-assignment
    # reward/tilt (small) and the cumulative balance decide WHO takes it, so
    # the post-night return steer can still avoid S4 on a first day back.
    for d in range(D):
        pres = model.NewBoolVar(f"s4pres_{d}")
        for r in R:
            model.Add(pres >= x[r.id, d, "S4"])
        model.Add(pres <= sum(x[r.id, d, "S4"] for r in R))
        terms.append(-W_S4_PRESENCE * pres)
    for r in R:
        for d in range(D):
            terms.append(-(W_S4_DAILY + W_S4_TILT * (3 - r.weight)) * x[r.id, d, "S4"])
    scum = []
    for r in R:
        sv = model.NewIntVar(0, D, f"s4_{r.id}")
        model.Add(sv == sum(x[r.id, d, "S4"] for d in range(D)))
        base = round(p.debt.get(r.id, {}).get("s4", 0))  # hundredths
        c = model.NewIntVar(-1_000_000, 1_000_000, f"scum_{r.id}")
        model.Add(c == 100 * sv + base)
        scum.append(c)
    s4mx = model.NewIntVar(-1_000_000, 1_000_000, "s4max")
    s4mn = model.NewIntVar(-1_000_000, 1_000_000, "s4min")
    for c in scum:
        model.Add(s4mx >= c)
        model.Add(s4mn <= c)
    terms.append(W_S4_CUM_SPREAD * (s4mx - s4mn))
    s4tgt = model.NewIntVar(-1_000_000, 1_000_000, "s4tgt")
    for i, c in enumerate(scum):
        dev = model.NewIntVar(0, 2_000_000, f"s4dev_{i}")
        model.AddAbsEquality(dev, c - s4tgt)
        terms.append(W_S4_CUM_ABS * dev)

    # ---- boundary lookahead (last day -> next cycle's first day) ----
    # The day after the last day starts the next cycle. An S2/S3 on the last
    # day restricts that resource's next day (Off/S3 after a night, no S1/S4
    # after an evening). Sending those shifts to people who rest on that
    # weekday anyway costs no next-day availability; sending them to anybody
    # else eats into the next cycle's Monday morning minimum, which arrives as
    # a fixed day-0 input and cannot be relaxed at generation time.
    next_wd = (p.start_date + timedelta(days=D)).weekday()
    for r in R:
        if next_wd not in r.off_days:
            terms.append(W_BOUNDARY_LOOKAHEAD
                         * (x[r.id, D - 1, "S2"] + x[r.id, D - 1, "S3"]))

    model.Minimize(_lsum(terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = p.time_limit_s
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return "INFEASIBLE", None
    assign = {}
    for r in R:
        seq = []
        for d in range(D):
            v = OFF
            for s in SHIFTS:
                if solver.Value(x[r.id, d, s]) == 1:
                    v = s
                    break
            seq.append(v)
        assign[r.id] = seq
    return ("OPTIMAL" if status == cp_model.OPTIMAL else "FEASIBLE"), assign


def generate(p: SolveParams) -> SolveResult:
    if cp_model is None:
        raise RuntimeError("ortools is not installed. Run: pip install ortools")
    R = [r for r in p.resources if r.active]
    days = [p.start_date + timedelta(days=i) for i in range(CYCLE_DAYS)]
    day_types = [day_type(d, p.light_days) for d in days]

    status, assign, level = "INFEASIBLE", None, 0
    for level in range(len(RELAX_LABELS) + 1):
        status, assign = _solve_once(p, level)
        if assign is not None:
            break
    if assign is None:
        return SolveResult(status="INFEASIBLE", days=days, day_types=day_types)

    relaxations = RELAX_LABELS[:level]
    violations = verify(assign, R, days, day_types, day0=p.day0,
                        day1_overrides=p.day1_overrides, relax_level=level,
                        prev_work_streak=p.prev_work_streak,
                        prev_night_streak=p.prev_night_streak)
    flags = list(violations)
    for i, msg in enumerate(relaxations):
        flags.append({"code": f"RELAX_{i + 1}", "severity": "warning", "message": msg})
    for d in range(CYCLE_DAYS):
        if day_types[d] == "workday":
            cnt = sum(1 for r in R if assign[r.id][d] in ("S1", "S4"))
            if cnt >= 4:
                flags.append({"code": "EXTRA_MORNING", "severity": "info", "day": d,
                              "message": "4th morning person added to lift the morning weight — review if not needed"})

    stats = compute_stats(assign, R, days, day_types)
    return SolveResult(status=status, relax_level=level, relaxations=relaxations,
                       assign=assign, days=days, day_types=day_types,
                       flags=flags, stats=stats)
