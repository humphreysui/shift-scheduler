"""Shift Scheduler — FastAPI app (local, single user).

Serves the single-page UI and the scheduling API. Run with:  python app.py
"""
from __future__ import annotations

import threading
import uuid
import webbrowser
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from models import (CYCLE_DAYS, MAX_RESOURCES, MIN_RESOURCES, OFF, SHIFTS,
                    SHIFT_INFO, VALUES, WEEKDAY_NAMES, next_monday)
from rules import streaks_from_tail
from scheduler import SolveParams, generate
from store import Store

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
DATA = BASE / "data"

app = FastAPI(title="Shift Scheduler")
store = Store(DATA / "project.json")


# ---------------------------------------------------------------- helpers
def _cycle_start() -> date:
    cs = store.project["settings"].get("cycle_start")
    if not cs:
        cs = next_monday().isoformat()
        store.project["settings"]["cycle_start"] = cs
        store.save()
    return date.fromisoformat(cs)


def _light_days() -> set:
    return set(store.project["settings"].get("light_days") or [])


def _prev_tail(start: date) -> dict:
    """Assignments of up to 6 days immediately before `start`, per resource.

    Follows the version in effect for that cycle (Store.tail_version), so a
    restored version wins over newer repair drafts - the same source of truth
    the ledger counts.
    """
    v = store.tail_version((start - timedelta(days=1)).isoformat())
    if v is None:
        return {}
    return {rid: list(arr[-6:]) for rid, arr in v["assign"].items()}


def _day0_effective(start: date, resources) -> dict:
    tail = _prev_tail(start)
    out = {}
    for r in resources:
        if r.id in store.project["day0_overrides"]:
            out[r.id] = store.project["day0_overrides"][r.id]
        elif r.id in tail and tail[r.id]:
            out[r.id] = tail[r.id][-1]
        else:
            out[r.id] = OFF
    return out


def _tails_for_streaks(start: date, resources) -> dict:
    tail = _prev_tail(start)
    out = {}
    for r in resources:
        vals = list(tail.get(r.id, []))
        over = store.project["day0_overrides"].get(r.id)
        if over is not None:
            if vals:
                vals[-1] = over
            else:
                vals = [over]
        elif not vals:
            vals = [OFF]
        out[r.id] = vals
    return out


def _debts() -> dict:
    led = store.ledger()
    return {rid: {"night": round(d["night_debt"] * 100),
                  "eve": round(d["eve_debt"] * 100),
                  "comp": round(d.get("comp_debt", 0.0) * 100),
                  "s4": round(d.get("s4_debt", 0.0) * 100),
                  "off": round(d.get("off_debt", 0.0) * 100)}
            for rid, d in led.items()}


def _validate_start_state(resources, day0_eff, day1_ov, wstreak, nstreak) -> list:
    errs = []
    for r in resources:
        v = day1_ov.get(r.id)
        if v is None:
            continue
        if v not in VALUES:
            errs.append(f"{r.name}: invalid day-1 value '{v}'.")
            continue
        if day0_eff.get(r.id) == "S3" and v not in (OFF, "S3"):
            errs.append(f"{r.name}: cannot work {v} right after a night shift "
                        f"— day 1 must be Off or S3.")
        if day0_eff.get(r.id) == "S2" and v in ("S1", "S4"):
            errs.append(f"{r.name}: cannot start {v} right after an evening shift "
                        f"(only ~9h rest) — day 1 must be Off, S2 or S3.")
        if v != OFF and wstreak.get(r.id, 0) >= 5:
            errs.append(f"{r.name}: already has {wstreak[r.id]} consecutive workdays "
                        f"before day 1 — day 1 must be a rest day.")
        if v == "S3" and nstreak.get(r.id, 0) >= 3:
            errs.append(f"{r.name}: already has {nstreak[r.id]} consecutive night shifts "
                        f"— a 4th in a row is not allowed.")
    return errs


def _resource_dict(r) -> dict:
    return asdict(r)


# ---------------------------------------------------------------- API
@app.get("/api/state")
def get_state():
    resources = store.resources()
    start = _cycle_start()
    version = store.current_version()
    days = version["days"] if version else \
        [(start + timedelta(days=i)).isoformat() for i in range(CYCLE_DAYS)]
    tail = _prev_tail(start)
    day0_auto = {rid: (vals[-1] if vals else OFF) for rid, vals in tail.items()}
    return {
        "settings": dict(store.project["settings"]),
        "resources": [_resource_dict(r) for r in resources],
        "day0": {"overrides": store.project["day0_overrides"],
                 "auto": day0_auto,
                 "effective": _day0_effective(start, resources)},
        "day1": {"overrides": store.project["day1_overrides"]},
        "current": version,
        "versions": list(reversed(store.versions_meta())),
        "ledger": store.ledger(),
        "cycle_start": start.isoformat(),
        "days": days,
        "shift_info": SHIFT_INFO,
        "weekdays": WEEKDAY_NAMES,
        "min_resources": MIN_RESOURCES,
        "max_resources": MAX_RESOURCES,
    }


@app.post("/api/resources")
def set_resources(payload: dict = Body(default={})):
    items = payload.get("resources") or []
    if len(items) > MAX_RESOURCES:
        raise HTTPException(400, f"At most {MAX_RESOURCES} resources are supported.")
    out, seen = [], set()
    for it in items:
        rid = (str(it.get("id") or "")).strip() or uuid.uuid4().hex[:8]
        if rid in seen:
            raise HTTPException(400, "Duplicate resource id.")
        seen.add(rid)
        name = (str(it.get("name") or "")).strip()
        if not name:
            raise HTTPException(400, "Every resource needs a name.")
        try:
            weight = int(it.get("weight", 2))
        except (TypeError, ValueError):
            raise HTTPException(400, f"Invalid weight for {name}.")
        if weight not in (1, 2, 3):
            raise HTTPException(400, f"{name}: skill weight must be 1, 2 or 3.")
        try:
            ostart = int(it.get("off_pair_start", 0)) % 7
        except (TypeError, ValueError):
            raise HTTPException(400, f"Invalid off-day pair for {name}.")
        out.append({"id": rid, "name": name, "weight": weight,
                    "off_pair_start": ostart, "active": bool(it.get("active", True))})
    store.set_resources(out)
    return {"ok": True, "resources": out}


@app.post("/api/settings")
def set_settings(payload: dict = Body(default={})):
    s = store.project["settings"]
    if "cycle_start" in payload and payload["cycle_start"]:
        try:
            date.fromisoformat(payload["cycle_start"])
        except ValueError:
            raise HTTPException(400, "Cycle start must be an ISO date (YYYY-MM-DD).")
        s["cycle_start"] = payload["cycle_start"]
    if "light_days" in payload:
        light = []
        for d in payload["light_days"] or []:
            try:
                date.fromisoformat(d)
            except (ValueError, TypeError):
                raise HTTPException(400, f"Invalid light day date: {d}")
            light.append(d)
        s["light_days"] = light
    if "priority" in payload:
        if payload["priority"] not in ("floor_first", "nights_first"):
            raise HTTPException(400, "priority must be 'floor_first' or 'nights_first'.")
        s["priority"] = payload["priority"]
    store.save()
    return {"ok": True, "settings": s}


@app.post("/api/startstate")
def set_start_state(payload: dict = Body(default={})):
    for key in ("day0", "day1"):
        vals = payload.get(key) or {}
        target = store.project["day0_overrides" if key == "day0" else "day1_overrides"]
        for rid, v in vals.items():
            if v is None or v == "":
                target.pop(rid, None)
                continue
            if v not in VALUES:
                raise HTTPException(400, f"Invalid value '{v}' (allowed: {', '.join(VALUES)}).")
            target[rid] = v
    store.save()
    return {"ok": True,
            "day0_overrides": store.project["day0_overrides"],
            "day1_overrides": store.project["day1_overrides"]}


def _check_resources_ready() -> list:
    resources = store.resources(active_only=True)
    if len(resources) < MIN_RESOURCES:
        raise HTTPException(400, f"At least {MIN_RESOURCES} active resources are required "
                                 f"(currently {len(resources)}). With fewer, the weekly "
                                 f"coverage minimum cannot be met.")
    return resources


@app.post("/api/generate")
def generate_cycle(payload: dict = Body(default={})):
    resources = _check_resources_ready()
    start = _cycle_start()
    light = _light_days()
    day0_eff = _day0_effective(start, resources)
    day1_ov = {k: v for k, v in store.project["day1_overrides"].items()
               if any(r.id == k for r in resources)}
    tails = _tails_for_streaks(start, resources)
    wstreak, nstreak = streaks_from_tail(tails)
    errs = _validate_start_state(resources, day0_eff, day1_ov, wstreak, nstreak)
    if errs:
        raise HTTPException(400, " | ".join(errs))
    params = SolveParams(
        resources=resources, start_date=start, light_days=light,
        day0=day0_eff, day1_overrides=day1_ov, debt=_debts(),
        prev_work_streak=wstreak, prev_night_streak=nstreak,
        priority=store.project["settings"].get("priority", "floor_first"))
    res = generate(params)
    if res.status == "INFEASIBLE":
        raise HTTPException(400, "No valid schedule found, even with relaxations. "
                                 "Check the off-day pairs and the starting state.")
    label = (payload.get("label") or "Full generation").strip()
    version = store.add_version(
        cycle_id=start.isoformat(), label=label,
        days=[d.isoformat() for d in res.days], assign=res.assign, flags=res.flags,
        stats=res.stats, relaxations=res.relaxations, day0_used=day0_eff,
        light_days=sorted(light))
    return {
        "ok": True,
        "version": {"id": version["id"], "label": version["label"]},
        "warnings": [f for f in res.flags if f["severity"] in ("warning", "error")],
        "info": [f for f in res.flags if f["severity"] == "info"],
        "relaxations": res.relaxations,
    }


@app.post("/api/repair")
def repair_cell(payload: dict = Body(default={})):
    v = store.current_version()
    if not v:
        raise HTTPException(400, "Generate a schedule first.")
    rid = payload.get("resource_id")
    try:
        day = int(payload.get("day"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid day index.")
    value = payload.get("value")
    if value not in VALUES:
        raise HTTPException(400, f"Invalid value. Allowed: {', '.join(VALUES)}.")
    if not (0 <= day < len(v["days"])):
        raise HTTPException(400, "Day index out of range.")
    assign = v["assign"]
    if rid not in assign:
        raise HTTPException(400, "Resource is not part of the current schedule.")
    old = assign[rid][day]
    if old == value:
        return {"ok": True, "noop": True}

    all_res = store.resources()
    active = [r for r in all_res if r.active]
    if set(r.id for r in active) != set(assign.keys()):
        raise HTTPException(400, "The resource list changed since this schedule was "
                                 "generated — run a full generation again.")
    name = next((r.name for r in all_res if r.id == rid), rid)
    start = date.fromisoformat(v["cycle_id"])
    light = set(v.get("light_days") or [])
    day0 = dict(v.get("day0_used") or {})

    if day == 0 and day0.get(rid) == "S3" and value not in (OFF, "S3"):
        raise HTTPException(400, "Cannot assign a day/evening shift right after a night shift.")
    if day == 0 and day0.get(rid) == "S2" and value in ("S1", "S4"):
        raise HTTPException(400, "Cannot start S1/S4 right after an evening shift "
                                 "(only ~9h rest) — pick Off, S2 or S3.")
    if day > 0 and assign[rid][day - 1] == "S3" and value not in (OFF, "S3"):
        raise HTTPException(400, "Cannot assign a day/evening shift right after a night shift.")
    if day > 0 and assign[rid][day - 1] == "S2" and value in ("S1", "S4"):
        raise HTTPException(400, "Cannot start S1/S4 right after an evening shift "
                                 "(only ~9h rest) — pick Off, S2 or S3.")
    if day == 0:
        tails = _tails_for_streaks(start, active)
        wstreak, nstreak = streaks_from_tail(tails)
        if value != OFF and wstreak.get(rid, 0) >= 5:
            raise HTTPException(400, "This would create a 6th consecutive workday.")
        if value == "S3" and nstreak.get(rid, 0) >= 3:
            raise HTTPException(400, "This would create a 4th consecutive night shift.")

    baseline = {k: list(arr) for k, arr in assign.items()}
    baseline[rid][day] = value
    tails = _tails_for_streaks(start, active)
    wstreak, nstreak = streaks_from_tail(tails)
    params = SolveParams(
        resources=active, start_date=start, light_days=light,
        day0=day0, day1_overrides={}, debt=_debts(),
        prev_work_streak=wstreak, prev_night_streak=nstreak,
        baseline=baseline, change_from_day=day, edited_resource=rid,
        priority=store.project["settings"].get("priority", "floor_first"))
    res = generate(params)
    if res.status == "INFEASIBLE":
        raise HTTPException(400, "Repair failed: no valid way to cover the schedule after "
                                 "this change. Try editing a different day or run a full "
                                 "generation.")

    changes = []
    for r in active:
        for d in range(len(v["days"])):
            if assign[r.id][d] != res.assign[r.id][d]:
                changes.append({
                    "resource_id": r.id, "name": r.name, "day": d,
                    "date": v["days"][d], "from": assign[r.id][d], "to": res.assign[r.id][d],
                    "other": r.id != rid,
                })
    label = f"Repair: {name} {old} → {value} on {v['days'][day]}"
    version = store.add_version(
        cycle_id=v["cycle_id"], label=label, days=v["days"], assign=res.assign,
        flags=res.flags, stats=res.stats, relaxations=res.relaxations,
        day0_used=day0, light_days=sorted(light))
    return {
        "ok": True,
        "version": {"id": version["id"], "label": version["label"]},
        "changes": changes,
        "warnings": [f for f in res.flags if f["severity"] in ("warning", "error")],
        "info": [f for f in res.flags if f["severity"] == "info"],
    }


@app.post("/api/restore")
def restore_version(payload: dict = Body(default={})):
    vid = payload.get("version_id")
    v = store.restore(vid) if vid else None
    if not v:
        raise HTTPException(404, "Version not found.")
    return {"ok": True, "version": {"id": v["id"], "label": v["label"]}}


def _diff(a: dict, b: dict) -> list:
    out = []
    names = {r.id: r.name for r in store.resources()}
    for rid, seq_b in b["assign"].items():
        seq_a = a["assign"].get(rid, [])
        for d in range(min(len(seq_a), len(seq_b))):
            if seq_a[d] != seq_b[d]:
                out.append({"resource_id": rid, "name": names.get(rid, rid), "day": d,
                            "date": b["days"][d], "from": seq_a[d], "to": seq_b[d]})
    return out


@app.get("/api/diff")
def diff_versions(from_id: str, to_id: str):
    a, b = store.version(from_id), store.version(to_id)
    if not a or not b:
        raise HTTPException(404, "Version not found.")
    return {"ok": True, "changes": _diff(a, b)}


@app.get("/api/export.csv")
def export_csv():
    v = store.current_version()
    if not v:
        raise HTTPException(400, "Nothing to export yet.")
    lines = []
    header = ["Name", "Weight", "Off pair"] + \
        [f"{WEEKDAY_NAMES[date.fromisoformat(d).weekday()]} {d}" for d in v["days"]]
    lines.append(",".join(header))
    comp = {}
    for st in (v["stats"].get("resources") or []):
        comp[st["id"]] = {c["day"] for c in (st.get("comp_days") or [])}
    for r in store.resources():
        seq = v["assign"].get(r.id)
        if seq is None:
            continue
        from models import off_pair_label
        cs = comp.get(r.id, set())
        cells = ["Off (comp)" if (val == "Off" and i in cs) else val
                 for i, val in enumerate(seq)]
        row = [r.name, str(r.weight), off_pair_label(r.off_pair_start)] + cells
        lines.append(",".join(row))
    return PlainTextResponse("\n".join(lines), media_type="text/csv",
                             headers={"Content-Disposition":
                                      "attachment; filename=shift-schedule.csv"})


@app.middleware("http")
async def _no_cache_static(request, call_next):
    """Local single-user app: static files change often — always revalidate."""
    resp = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".css", ".js", ".html")):
        resp.headers.setdefault("Cache-Control", "no-cache")
    return resp


app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")


def _open_browser():
    import time
    time.sleep(1.2)
    webbrowser.open("http://127.0.0.1:8765")


if __name__ == "__main__":
    import uvicorn
    threading.Thread(target=_open_browser, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
