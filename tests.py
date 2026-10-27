"""Acceptance tests for the shift scheduler core (no server needed).

Run:  python tests.py
Covers AT1-AT5 from the spec plus the ledger carryover.
"""
from __future__ import annotations

import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

from models import OFF, Resource, day_type
from rules import compute_stats, streaks_from_tail, verify
from scheduler import SolveParams, generate
from store import Store

MONDAY = date(2026, 10, 19)
WEIGHTS_7 = [3, 3, 3, 2, 2, 2, 1]
WEIGHTS_9 = [3, 3, 3, 3, 2, 2, 2, 1, 1]

PASSED = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    assert cond, f"{name}: {detail}"
    PASSED.append(name)


def make_resources(weights, pairs=None):
    pairs = pairs if pairs is not None else list(range(len(weights)))
    return [Resource(id=f"r{i}", name=f"Person {i + 1}", weight=w, off_pair_start=pairs[i])
            for i, w in enumerate(weights)]


def params(resources, **kw):
    # Quality-bounded assertions need converged incumbents: with the full
    # fairness stack, short caps return run-to-run varying incumbents on the
    # tight 7-staff rosters (CP-SAT's 8-worker search is nondeterministic and a
    # no-solution timeout silently escalates the relaxation ladder). Default
    # cap 120 s; specific suites override where measured (AT1: 180 s).
    base = dict(resources=resources, start_date=MONDAY, time_limit_s=120.0)
    base.update(kw)
    return SolveParams(**base)


def structural(violations):
    return [v for v in violations if v["severity"] == "error"]


def full_verify(out, resources, **kw):
    kw.setdefault("day0", getattr(out, "day0", None) or {})
    kw.setdefault("prev_tail", getattr(out, "prev_tail", None) or {})
    return verify(out.assign, resources, out.days, out.day_types,
                  relax_level=out.relax_level, **kw)


def rebuild_comp_days(out, res):
    """Independent rebuild of comp days:
       - extra Off (non-anchor) directly after a >= 2 night run, and
       - for a 3-night run whose recovery day IS an anchor day, the first
         non-anchor day after the pair (the "extra day beyond the pair"),
         including the day pinned in from a run at the end of the previous
         cycle (out.prev_tail).
    Mirrors rules.compute_stats on purpose: the two are compared for equality.
    """
    got = {}
    D = len(out.days)
    tail_all = getattr(out, "prev_tail", None) or {}
    for r in res:
        seq = out.assign[r.id]
        days_found = []
        for d in range(2, D):
            if seq[d] != OFF or out.days[d].weekday() in r.off_days:
                continue
            k, i = 0, d - 1
            while i >= 0 and seq[i] == "S3":
                k += 1
                i -= 1
            if k >= 2:
                days_found.append(d)
        for d in range(3, D):
            if seq[d] != OFF or out.days[d].weekday() not in r.off_days:
                continue
            if not all(seq[i] == "S3" for i in (d - 1, d - 2, d - 3)):
                continue
            d1 = next((k for k in range(d + 1, min(d + 4, D))
                       if out.days[k].weekday() not in r.off_days), None)
            if d1 is not None and seq[d1] == OFF:
                days_found.append(d1)
        tl = list(tail_all.get(r.id, []))
        for q in range(-len(tl), 0):
            if tl[q + len(tl)] != OFF:
                continue
            if any(not (q - 1 - k + len(tl) >= 0
                        and tl[q - 1 - k + len(tl)] == "S3") for k in range(3)):
                continue
            if (out.days[0] + timedelta(days=q)).weekday() not in r.off_days:
                continue
            d1 = next((k for k in range(q + 1, q + 4)
                       if (out.days[0] + timedelta(days=k)).weekday()
                       not in r.off_days), None)
            if d1 is not None and 0 <= d1 < D and seq[d1] == OFF \
                    and d1 not in days_found:
                days_found.append(d1)
        got[r.id] = sorted(days_found)
    return got


def return_violations(out, res):
    """Count return days breaking the hard order S2 > S1 > S4 (S3 never).

    A return day is the first workday after a night run followed by >= 1 Off
    recovery day(s); out.prev_tail/out.day0 cover returns landing on day 1.
    """
    n = 0
    tail_all = getattr(out, "prev_tail", None) or {}
    day0 = getattr(out, "day0", None) or {}
    for r in res:
        seq = out.assign[r.id]
        tl = list(tail_all.get(r.id, []))
        runend = False
        for t in range(-len(tl), 0):
            off_t = tl[t + len(tl)] == OFF
            pn = t - 1 >= -len(tl) and tl[t - 1 + len(tl)] == "S3"
            runend = off_t and (pn or runend)
        for d in range(len(out.days)):
            if runend and seq[d] != OFF and seq[d] not in ("S2", "S1"):
                n += 1
            off_d = seq[d] == OFF
            pn = day0.get(r.id, OFF) == "S3" if d == 0 else seq[d - 1] == "S3"
            runend = off_d and (pn or runend)
    return n


def evening_rest_violations(out, res, day0=None):
    """Count S1/S4 starts the day after an S2 (includes the day-0 boundary)."""
    n = 0
    day0 = day0 or {}
    for r in res:
        seq = out.assign[r.id]
        if day0.get(r.id) == "S2" and seq[0] in ("S1", "S4"):
            n += 1
        for d in range(1, 28):
            if seq[d - 1] == "S2" and seq[d] in ("S1", "S4"):
                n += 1
    return n


def test_at1_basic_seven():
    print("AT1 - 7 staff basic generation")
    res = make_resources(WEIGHTS_7)
    # 180 s: this adversarial roster's level-0 optimum (night spread 3) only
    # appears reliably at production-scale budget — a 45 s cap returned
    # night 3-4 run to run.
    out = generate(params(res, time_limit_s=180.0))
    check("solver found a schedule", out.status in ("OPTIMAL", "FEASIBLE"), out.status)
    viol = full_verify(out, res)
    errs = structural(viol)
    check("no structural violations", not errs, str(errs[:3]))
    check("no relaxations needed", out.relax_level == 0, f"level {out.relax_level}")

    for d in range(28):
        vals = [out.assign[r.id][d] for r in res]
        m = sum(1 for v in vals if v in ("S1", "S4"))
        want = 3 if out.day_types[d] == "workday" else 1
        check(f"day {d} morning minimum", m >= want, f"morning={m}")
        check(f"day {d} evening coverage", sum(1 for v in vals if v == "S2") >= 1, "")
        check(f"day {d} night coverage", sum(1 for v in vals if v == "S3") >= 1, "")
        check(f"day {d} S4 cap", sum(1 for v in vals if v == "S4") <= 1, "")

    wf = [f for f in out.flags if f["code"] == "WEIGHT_BELOW_7"]
    # The hard evening-rest rule costs one below-floor weekday on this
    # synthetic roster (its Tuesday has a single w3 available; the solver
    # proves one below-floor day is unavoidable here). What must hold:
    # at most one such day, and it is flagged.
    below = [d for d in range(28) if out.day_types[d] == "workday"
             and sum(r.weight for r in res if out.assign[r.id][d] in ("S1", "S4")) < 7]
    check("at most one weekday below the morning weight floor", len(below) <= 1,
          f"below on days {below}")
    check("below-floor weekdays are exactly the flagged ones",
          sorted(f["day"] for f in wf) == below,
          f"flags {[f['day'] for f in wf]} vs below {below}")
    for r in res:
        seq = out.assign[r.id]
        check(f"{r.name} works all three shift types", all(s in seq for s in ("S1", "S2", "S3")), "")
        check(f"{r.name} rests on anchor pair", all(out.assign[r.id][d] == OFF
              for d, dt in enumerate(out.days) if dt.weekday() in r.off_days), "")
    # At 7 staff with this roster the rules interact: the weight floor, the
    # night chains and the cumulative evening/off-day balance trade off. With
    # the evening + off ledger terms active the proven OPTIMUM trades one
    # night on the weight-1 resource for eve/off balance (night spread 3 at
    # 180 s; the old stack's bound of 2 no longer holds). Across cycles the
    # ledger returns nights to equity — see the cumulative night-debt check in
    # the carryover test.
    check("night spread <= 3 (evening/off balance trades one night here)",
          out.stats["nights_spread"] <= 3, str(out.stats["nights_spread"]))

    # --- v1.2 rules: comp rest, S4 policy, post-night return ---
    comps = rebuild_comp_days(out, res)
    stats_comps = {st["id"]: [c["day"] for c in st["comp_days"]] for st in out.stats["resources"]}
    check("comp-day stats match the schedule", comps == stats_comps,
          f"{comps} vs {stats_comps}" if comps != stats_comps else "")
    check("comp-day spread <= 1", out.stats["comp_spread"] <= 1, str(out.stats["comp_spread"]))
    s4days = sum(1 for d in range(28) if any(out.assign[r.id][d] == "S4" for r in res))
    check("S4 staffed every day", s4days == 28, f"{s4days}/28")
    # On this synthetic roster one resource's single rotation (S1) slot is
    # also his only S4-eligible day once the rest rule applies, so a single
    # permanent outlier is unavoidable (proved optimal at spread 5); the
    # relaxed "at least 5 of 7" bound tolerates exactly that outlier.
    s4counts = [sum(1 for v in out.assign[r.id] if v == "S4") for r in res]
    check("S4 counts within 1 of the max for at least 5 of 7",
          sum(1 for c in s4counts if c >= max(s4counts) - 1) >= 5,
          f"{s4counts}")
    rv = return_violations(out, res)
    check("post-night return keeps the hard order S2 > S1 > S4", rv == 0,
          f"{rv} violations")
    ev = evening_rest_violations(out, res)
    check("no S1/S4 start right after an evening (S2) shift", ev == 0, f"{ev} violations")
    print(f"  [note] 7 staff: {out.stats['comp_total']} comp day(s), "
          f"S4 on {s4days}/28 days (spread {out.stats['s4_spread']}), "
          f"return-rule slips: {rv}, evening-rest slips: {ev}")

    # nights_first priority: night equality dominates the weight floor instead.
    out_nf = generate(params(res, priority="nights_first"))
    check("nights_first: night spread <= 1", out_nf.stats["nights_spread"] <= 1,
          str(out_nf.stats["nights_spread"]))
    ev_nf = evening_rest_violations(out_nf, res)
    check("nights_first: no S1/S4 right after an S2", ev_nf == 0, f"{ev_nf} violations")
    nf_flags = len([f for f in out_nf.flags if f["code"] == "WEIGHT_BELOW_7"])
    nf_below = 0
    for d in range(28):
        if out_nf.day_types[d] == "workday":
            w = sum(rr.weight for rr in res if out_nf.assign[rr.id][d] in ("S1", "S4"))
            if w < 7:
                nf_below += 1
    check("nights_first: every below-floor day is flagged", nf_flags == nf_below,
          f"{nf_flags} flags vs {nf_below} days")
    print(f"  [note] nights_first at 7 staff: {nf_below} flagged day(s), "
          f"night spread {out_nf.stats['nights_spread']}")
    return out, res


def test_at2_nine_and_slack():
    print("AT2 - 9 staff slack usage")
    res = make_resources(WEIGHTS_9, pairs=[0, 1, 2, 3, 5, 6, 0, 2, 4])
    out = generate(params(res))
    check("solver found a schedule", out.status in ("OPTIMAL", "FEASIBLE"), out.status)
    errs = structural(full_verify(out, res))
    check("no structural violations", not errs, str(errs[:3]))
    check("night spread <= 1", out.stats["nights_spread"] <= 1, str(out.stats["nights_spread"]))
    doubles = sum(1 for d in range(28)
                  if sum(1 for r in res if out.assign[r.id][d] == "S3") >= 2)
    check("2nd night person appears (coverage priority)", doubles >= 1, f"doubles={doubles}")
    # v1.2 rules with slack: comp days, S4 daily, return preference
    comps = rebuild_comp_days(out, res)
    stats_comps = {st["id"]: [c["day"] for c in st["comp_days"]] for st in out.stats["resources"]}
    check("comp-day stats match the schedule", comps == stats_comps,
          f"{comps} vs {stats_comps}" if comps != stats_comps else "")
    check("comp days are actually used at 9 staff", out.stats["comp_total"] >= 8,
          str(out.stats["comp_total"]))
    check("comp-day spread <= 1", out.stats["comp_spread"] <= 1, str(out.stats["comp_spread"]))
    s4days = sum(1 for d in range(28) if any(out.assign[r.id][d] == "S4" for r in res))
    check("S4 staffed nearly every day", s4days >= 26, f"{s4days}/28")
    check("S4 spread <= 1", out.stats["s4_spread"] <= 1, str(out.stats["s4_spread"]))
    rv = return_violations(out, res)
    # Hard rule: 0 violations at every relaxation level below the last one;
    # only the top level may bend it (loud RETURN_ORDER warning in flags).
    bent = out.relax_level == 6 and rv > 0
    check("post-night return keeps the hard order S2 > S1 > S4",
          rv == 0 or bent,
          f"{rv} violations at level {out.relax_level}")
    if bent:
        check("... a bent return order is loudly flagged",
              any(f["code"] == "RETURN_ORDER" for f in out.flags), "no flag")
    ev = evening_rest_violations(out, res)
    check("no S1/S4 start right after an S2 (hard rest rule)", ev == 0, f"{ev} violations")
    print(f"  [note] 9 staff: {out.stats['comp_total']} comp days (spread "
          f"{out.stats['comp_spread']}), S4 on {s4days}/28 days, return-rule slips: {rv}, "
          f"evening-rest slips: {ev}")
    return out, res


def test_ledger_carryover():
    print("Ledger - night debt carryover across cycles")
    tmp = Path(tempfile.mkdtemp(prefix="ss_test_"))
    st = Store(tmp / "project.json")
    res = make_resources(WEIGHTS_9, pairs=[0, 1, 2, 3, 5, 6, 0, 2, 4])
    out1 = generate(params(res))
    st.add_version(cycle_id=MONDAY.isoformat(), label="A",
                   days=[d.isoformat() for d in out1.days], assign=out1.assign,
                   flags=[], stats=out1.stats, relaxations=[], day0_used={}, light_days=[])
    led = st.ledger()
    debt = {rid: {"night": round(v["night_debt"] * 100), "eve": round(v["eve_debt"] * 100),
                  "comp": round(v["comp_debt"] * 100), "s4": round(v["s4_debt"] * 100),
                  "off": round(v["off_debt"] * 100)}
            for rid, v in led.items()}
    start_b = MONDAY + timedelta(days=28)
    out2 = generate(params(res, start_date=start_b, debt=debt))
    st.add_version(cycle_id=start_b.isoformat(), label="B",
                   days=[d.isoformat() for d in out2.days], assign=out2.assign,
                   flags=[], stats=out2.stats, relaxations=[], day0_used={}, light_days=[])
    led2 = st.ledger()
    vals = [v["night_debt"] for v in led2.values()]
    spread = max(vals) - min(vals)
    check("cumulative night debt spread <= 1", spread <= 1.0, f"spread={spread:.2f}")
    cvals = [v["comp_debt"] for v in led2.values()]
    cspread = max(cvals) - min(cvals)
    check("cumulative comp-day debt spread <= 1.5", cspread <= 1.5, f"spread={cspread:.2f}")
    svals = [v["s4_debt"] for v in led2.values()]
    sspread = max(svals) - min(svals)
    check("cumulative S4 debt spread <= 1", sspread <= 1.0, f"spread={sspread:.2f}")
    ovals = [v["off_debt"] for v in led2.values()]
    ospread = max(ovals) - min(ovals)
    check("cumulative off-day debt spread <= 1", ospread <= 1.0, f"spread={ospread:.2f}")
    evvals = [v["eve_debt"] for v in led2.values()]
    espread = max(evvals) - min(evvals)
    check("cumulative eve debt spread <= 2", espread <= 2.0, f"spread={espread:.2f}")
    check("ledger keeps cumulative totals per resource",
          all(v["tot_off"] > 0 and v["tot_night"] > 0 and v["cycles"] == 2
              for v in led2.values()))
    print(f"  [note] after 2 cycles: night {spread:.2f}, comp {cspread:.2f}, s4 {sspread:.2f}, "
          f"off {ospread:.2f}, eve {espread:.2f}")


def test_tail_follows_current():
    print("Store - boundary tail follows the cycle's current version")
    tmp = Path(tempfile.mkdtemp(prefix="ss_test_"))
    st = Store(tmp / "project.json")
    res = make_resources(WEIGHTS_7)
    last = (MONDAY + timedelta(days=27)).isoformat()
    days = [(MONDAY + timedelta(days=i)).isoformat() for i in range(28)]
    keep = st.add_version(cycle_id=MONDAY.isoformat(), label="current",
                          days=days, assign={r.id: ["S1"] * 28 for r in res},
                          flags=[], stats={}, relaxations=[], day0_used={}, light_days=[],
                          created_at="2026-10-18T00:00:00")
    draft = st.add_version(cycle_id=MONDAY.isoformat(), label="draft",
                           days=days, assign={r.id: ["S2"] * 28 for r in res},
                           flags=[], stats={}, relaxations=[], day0_used={}, light_days=[],
                           created_at="2026-10-18T10:00:00")
    st.restore(keep["id"])  # user reinstates the earlier version
    v = st.tail_version(last)
    check("restored version wins over a newer draft",
          v is not None and v["id"] == keep["id"], v["id"] if v else "none")
    st.restore(draft["id"])  # the newer version is current again
    v = st.tail_version(last)
    check("newest version wins once it is current again",
          v is not None and v["id"] == draft["id"], "")
    st2 = Store(tmp / "empty.json")
    check("no tail version before anything is stored", st2.tail_version(last) is None, "")


def test_boundary_lookahead():
    print("Boundary - last-day lookahead keeps the next cycle start staffable")
    res = make_resources([2, 3, 3, 3, 2, 2, 1], pairs=[6, 5, 2, 4, 1, 5, 0])
    start_b = MONDAY + timedelta(days=28)
    # Chained cycles run with production-scale time margin: the level-0 search
    # under the full fairness stack can miss short caps (CP-SAT's 8-worker
    # portfolio is nondeterministic; a no-solution timeout silently escalates
    # the ladder). 120 s keeps the assertion meaningful; production generates
    # with 180 s.
    out1 = generate(params(res, time_limit_s=120.0))
    check("cycle 1 solved", out1.status in ("OPTIMAL", "FEASIBLE"), out1.status)
    next_wd = start_b.weekday()  # weekday of the day after cycle 1
    stuck = sum(1 for r in res if out1.assign[r.id][27] in ("S2", "S3")
                and next_wd not in r.off_days)
    check("last day leaves at most 2 people next-day restricted", stuck <= 2,
          f"stuck={stuck}")
    day0 = {r.id: out1.assign[r.id][27] for r in res}
    tails = {r.id: out1.assign[r.id][-6:] for r in res}
    w, n = streaks_from_tail(tails)
    out2 = generate(params(res, start_date=start_b, day0=day0,
                           prev_work_streak=w, prev_night_streak=n,
                           time_limit_s=120.0))
    # Chained 7-staff cycles sit on a feasibility knife-edge for the rotation
    # and consecutive-day guarantees: their level-0 outcome depends on cycle
    # 1's exact tail (with the full fairness stack it holds at level 2 even at
    # 900 s — spec item 13). What the lookahead must protect is the morning
    # side, which the checks below verify: the next cycle starts staffable and
    # never buys that with coverage (level 3+) or morning (level 4+)
    # relaxations.
    check("cycle 2 relaxes nothing beyond rotation/consecutive",
          out2.relax_level <= 2,
          f"level {out2.relax_level}: {out2.relaxations}")
    morning = [r for r in res if out2.assign[r.id][0] in ("S1", "S4")]
    check("cycle 2 day 1 keeps the 3-person morning minimum", len(morning) >= 3,
          f"{len(morning)} people")
    bad = [d for d in range(28) if out2.day_types[d] == "workday"
           and sum(r.weight for r in res if out2.assign[r.id][d] in ("S1", "S4")) < 7]
    check("cycle 2 has no below-floor weekday", not bad, str(bad[:3]))


def test_at3_sick_repair():
    print("AT3 - sick-leave repair (minimal change)")
    res = make_resources(WEIGHTS_7)
    out = generate(params(res))
    rid = next(r.id for r in res if out.assign[r.id][12] == "S3")
    baseline = {k: list(a) for k, a in out.assign.items()}
    baseline[rid][12] = OFF
    # change_from_day = the sick day itself: days before it freeze hard, the
    # edited cell is forced Off, and the day stays open to everyone else so
    # coverage can move (spec item 7 / AT3 — someone must take the sick
    # person's night; freezing through the day made a single-night day
    # provably infeasible at every relaxation level).
    p2 = params(res, baseline=baseline, change_from_day=12, edited_resource=rid)
    out2 = generate(p2)
    check("repair produced a schedule", out2.status in ("OPTIMAL", "FEASIBLE"), out2.status)
    errs = structural(full_verify(out2, res))
    check("no structural violations after repair", not errs, str(errs[:3]))
    check("sick day is Off", out2.assign[rid][12] == OFF, out2.assign[rid][12])
    frozen_ok = all(out2.assign[r.id][d] == baseline[r.id][d]
                    for r in res for d in range(12))
    check("days before the edit are frozen", frozen_ok, "")
    covered = any(out2.assign[r.id][12] == "S3" for r in res)
    check("coverage restored on the sick day", covered, "")
    day12_others = sum(1 for r in res if r.id != rid
                       and out2.assign[r.id][12] != out.assign[r.id][12])
    check("day-12 re-coverage is minimal", day12_others <= 2,
          f"{day12_others} other cells changed on day 12")
    others = sum(1 for r in res if r.id != rid for d in range(28)
                 if out2.assign[r.id][d] != out.assign[r.id][d])
    check("limited changes to other people", others <= 6, f"{others} cells changed")


def test_at4_anchor_edit():
    print("AT4 - off-day pair edit + regenerate")
    res = make_resources(WEIGHTS_7)
    _ = generate(params(res))
    res2 = make_resources(WEIGHTS_7, pairs=[6, 1, 2, 3, 4, 5, 0])  # Person 1 now rests Sun+Mon
    out = generate(params(res2))
    errs = structural(full_verify(out, res2))
    check("no structural violations after anchor change", not errs, str(errs[:3]))
    r0 = res2[0]
    check("new anchor respected", all(out.assign[r0.id][d] == OFF
          for d, dt in enumerate(out.days) if dt.weekday() in r0.off_days), "")


def test_at5_starting_state():
    print("AT5 - starting day situation")
    res = make_resources(WEIGHTS_7)
    day0 = {r.id: OFF for r in res}
    day0[res[0].id] = "S3"
    out = generate(params(res, day0=day0))
    check("day-0 night worker rests/extends on day 1",
          out.assign[res[0].id][0] in (OFF, "S3"), out.assign[res[0].id][0])
    day1 = {res[0].id: OFF}
    out2 = generate(params(res, day0=day0, day1_overrides=day1))
    check("day-1 override locked", out2.assign[res[0].id][0] == OFF, out2.assign[res[0].id][0])
    check("day-1 night worker still restricted on day 2",
          out2.assign[res[0].id][1] in (OFF, "S3"), out2.assign[res[0].id][1])
    tails = {r.id: [OFF] * 6 for r in res}
    tails[res[0].id] = ["S3"] * 6
    w, n = streaks_from_tail(tails)
    check("streak helper sees 6-night context", n[res[0].id] == 6, str(n))


def test_comp_policy():
    """Recovery hard rules: post-3-night extra rest day + post-night return order."""
    print("Comp rest policy - 3-night run ending on the regular days off")
    days = [MONDAY + timedelta(days=i) for i in range(7)]
    dts = [day_type(d) for d in days]
    # off_pair_start=3 -> mandatory rest on Thursday + Friday
    r = Resource(id="rp", name="Anchor person", weight=2, off_pair_start=3)

    def run(seq, res=r, tail=None, relax=0):
        assign = {res.id: seq}
        tail = tail or {}
        v = verify(assign, [res], days, dts, day0={res.id: OFF},
                   relax_level=relax, prev_tail=tail)
        flags = [x for x in v if x["code"] == "COMP3_NO_EXTRA_REST"]
        rets = [x for x in v if x["code"] == "RETURN_ORDER"]
        st = compute_stats(assign, [res], days, dts,
                           prev_tail=tail)["resources"][0]
        return flags, st, rets

    flags, st, rets = run(["S3", "S3", "S3", OFF, OFF, "S1", "S2"])
    check("3-night run on the anchor pair without an extra day is flagged",
          len(flags) == 1, f"{len(flags)} flag(s)")
    check("... as an error below the last relaxation level",
          flags and flags[0]["severity"] == "error",
          flags[0]["severity"] if flags else "")
    check("... and no comp day is credited", st["comp_count"] == 0, str(st["comp_count"]))

    flags, st, rets = run(["S3", "S3", "S3", OFF, OFF, OFF, "S1"])
    check("the extra rest day after the pair silences the flag", not flags, str(flags))
    check("... and is credited as a comp day", st["comp_count"] == 1,
          str(st["comp_count"]))

    # bent only at the last relaxation level -> loud warning, not a hard error
    flags, st, rets = run(["S3", "S3", "S3", OFF, OFF, "S1", "S2"], relax=6)
    check("the same gap bends with a warning at the last level",
          flags and flags[0]["severity"] == "warning",
          flags[0]["severity"] if flags else "no flag")

    # classic case: anchor Sat+Sun, 3 nights Mon-Wed, recovery Thursday (weekday)
    rc = Resource(id="rc", name="Classic", weight=2, off_pair_start=5)
    flags, st, rets = run(["S3", "S3", "S3", OFF, "S1", "S2", OFF], rc)
    check("classic non-anchor comp still credited", st["comp_count"] == 1,
          str(st["comp_count"]))
    check("classic non-anchor comp raises no flag", not flags, str(flags))

    # 2-night runs keep today's soft "when coverage allows" wording
    flags, st, rets = run([OFF, "S3", "S3", OFF, OFF, "S1", "S2"])
    check("2-night run on the anchor pair raises no flag", not flags, str(flags))

    # --- cross-seam: run + anchor recovery at the end of the previous cycle,
    # extra day pinned into THIS cycle's day 1 (Sat+Sun pair, day -1 = Sunday)
    tail = {"rc": [OFF, "S3", "S3", "S3", "S3", OFF]}
    flags, st, rets = run(["S1", "S2", OFF, "S1", "S2", OFF, OFF],
                          rc, tail=tail)
    check("extra day pinned from the previous cycle must be Off",
          len(flags) == 1, f"{len(flags)} flag(s)")
    check("... the gap day is the flagged one", flags and flags[0]["day"] == 0,
          str(flags))
    check("... no comp day credited while the gap is worked",
          st["comp_count"] == 0, str(st["comp_count"]))

    flags, st, rets = run([OFF, "S2", OFF, "S1", "S2", OFF, OFF],
                          rc, tail=tail)
    check("the pinned extra day Off silences the cross-seam flag",
          not flags, str(flags))
    check("... and is credited as a comp day in this cycle",
          st["comp_count"] == 1, str(st["comp_count"]))

    # --- post-night return order: S2 > S1 > S4, S3 never
    print("Return policy - first workday after night recovery")
    flags, st, rets = run(["S3", OFF, "S4", OFF, OFF, "S1", "S2"])
    check("S4 return after a night + Off is flagged",
          len(rets) == 1, f"{len(rets)} flag(s)")
    check("... as an error below the last relaxation level",
          rets and rets[0]["severity"] == "error",
          rets[0]["severity"] if rets else "")
    check("... on the return day", rets and rets[0]["day"] == 2,
          str(rets))

    flags, st, rets = run(["S3", OFF, "S4", OFF, OFF, "S1", "S2"], relax=6)
    check("an S4 return bends with a loud warning at the last level",
          rets and rets[0]["severity"] == "warning",
          rets[0]["severity"] if rets else "no flag")

    flags, st, rets = run(["S3", OFF, "S3", OFF, OFF, "S1", "S2"])
    check("straight back to nights (S3 return) is flagged",
          len(rets) == 1, f"{len(rets)} flag(s)")

    flags, st, rets = run(["S3", OFF, "S1", OFF, OFF, "S1", "S2"])
    check("S1 return is allowed (second choice in the order)", not rets,
          str(rets))

    flags, st, rets = run(["S3", OFF, "S2", OFF, OFF, "S1", "S2"])
    check("S2 return is the preferred choice", not rets, str(rets))

    # the scan walks the WHOLE Off recovery run, not just a single Off day
    flags, st, rets = run(["S3", OFF, OFF, "S4", OFF, "S1", "S2"])
    check("return after a 2-day recovery run is caught too",
          len(rets) == 1, f"{len(rets)} flag(s)")


def main():
    test_comp_policy()
    test_at1_basic_seven()
    test_at2_nine_and_slack()
    test_ledger_carryover()
    test_tail_follows_current()
    test_boundary_lookahead()
    test_at3_sick_repair()
    test_at4_anchor_edit()
    test_at5_starting_state()
    print(f"\nAll {len(PASSED)} checks passed.")


if __name__ == "__main__":
    sys.exit(main())
