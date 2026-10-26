"""JSON persistence: project file, versions, ledger, day-0/day-1 state."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from models import OFF, Resource


def default_project() -> dict:
    return {
        "format": 1,
        "settings": {"cycle_start": None, "light_days": [],
                      "priority": "floor_first"},
        "resources": [],
        "day0_overrides": {},   # rid -> explicit "yesterday" value (else auto from last version)
        "day1_overrides": {},   # rid -> locked first-day value
        "current_version_id": None,
        "cycle_current": {},    # cycle_id -> version id (source of truth for the ledger)
        "versions": [],
    }


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.project = self._load()

    # ---------- io ----------
    def _load(self) -> dict:
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        p = default_project()
        self._write(p)
        return p

    def _write(self, project: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(project, f, indent=1)
        os.replace(tmp, self.path)

    def save(self) -> None:
        self._write(self.project)

    # ---------- resources ----------
    def resources(self, active_only: bool = False) -> list[Resource]:
        out = [Resource(**r) for r in self.project["resources"]]
        return [r for r in out if r.active] if active_only else out

    def set_resources(self, resources: list[dict]) -> None:
        self.project["resources"] = resources
        self.save()

    # ---------- versions ----------
    def add_version(self, *, cycle_id: str, label: str, days: list[str], assign: dict,
                    flags: list, stats: dict, relaxations: list, day0_used: dict,
                    light_days: list, created_at: str | None = None) -> dict:
        vid = "v_" + uuid.uuid4().hex[:10]
        version = {
            "id": vid,
            "cycle_id": cycle_id,
            "label": label,
            "created_at": created_at or datetime.now().isoformat(timespec="seconds"),
            "days": days,
            "assign": assign,
            "flags": flags,
            "stats": stats,
            "relaxations": relaxations,
            "day0_used": day0_used,
            "light_days": light_days,
        }
        self.project["versions"].append(version)
        self.project["current_version_id"] = vid
        self.project["cycle_current"][cycle_id] = vid
        self.save()
        return version

    def current_version(self) -> dict | None:
        vid = self.project.get("current_version_id")
        return self.version(vid) if vid else None

    def version(self, vid: str) -> dict | None:
        for v in self.project["versions"]:
            if v["id"] == vid:
                return v
        return None

    def restore(self, vid: str) -> dict | None:
        v = self.version(vid)
        if v:
            self.project["current_version_id"] = vid
            self.project["cycle_current"][v["cycle_id"]] = vid
            self.save()
        return v

    def tail_version(self, end_day: str) -> dict | None:
        """Version whose schedule ends on `end_day`, preferring the entry in
        effect for its cycle (cycle_current), so a restored version wins over
        newer repair drafts. Falls back to the most recently created one.
        """
        cands = [v for v in self.project["versions"]
                 if v.get("days") and v["days"][-1] == end_day]
        if not cands:
            return None
        cur = [v for v in cands
               if self.project["cycle_current"].get(v["cycle_id"]) == v["id"]]
        return max(cur or cands, key=lambda v: v["created_at"])

    def versions_meta(self) -> list[dict]:
        out = []
        for v in self.project["versions"]:
            out.append({
                "id": v["id"], "label": v["label"], "created_at": v["created_at"],
                "cycle_id": v["cycle_id"],
                "warnings": len([f for f in v["flags"] if f.get("severity") == "warning"]),
                "relaxations": len(v["relaxations"]),
                "nights_spread": v["stats"].get("nights_spread"),
            })
        return out

    # ---------- ledger ----------
    def ledger(self) -> dict:
        """Cumulative fairness debt + running totals per resource.

        Uses the current version of each cycle (see cycle_current), so the
        accounting follows the schedule actually in effect (repairs included)
        and survives forever as long as the versions are kept.
        debt = sum over cycles of (count - fair_share). Positive = did too many.
        Keys: night_debt, eve_debt, comp_debt (comp days received),
        s4_debt (support shifts), off_debt (total rest days incl. comps), and
        tot_night / tot_eve / tot_comp / tot_s4 / tot_off cumulative counts
        since the first stored cycle, plus cycles.
        """
        empty = {"night_debt": 0.0, "eve_debt": 0.0, "comp_debt": 0.0,
                 "s4_debt": 0.0, "off_debt": 0.0, "cycles": 0,
                 "tot_night": 0, "tot_eve": 0, "tot_comp": 0, "tot_s4": 0,
                 "tot_off": 0}
        by_id = {v["id"]: v for v in self.project["versions"]}
        acc: dict[str, dict] = {}
        for cycle_id, vid in self.project["cycle_current"].items():
            v = by_id.get(vid)
            if not v:
                continue
            per = v["stats"].get("resources", [])
            counts_n = {r["id"]: r["counts"]["S3"] for r in per}
            counts_e = {r["id"]: r["counts"]["S2"] for r in per}
            counts_c = {r["id"]: r.get("comp_count", 0) for r in per}
            counts_s4 = {r["id"]: r["counts"].get("S4", 0) for r in per}
            counts_o = {r["id"]: r.get("off_days", 0) for r in per}
            for counts, key in ((counts_n, "night_debt"), (counts_e, "eve_debt"),
                                (counts_c, "comp_debt"), (counts_s4, "s4_debt"),
                                (counts_o, "off_debt")):
                total = sum(counts.values())
                r_count = len(counts) or 1
                for rid, c in counts.items():
                    d = acc.setdefault(rid, dict(empty))
                    d[key] += c - total / r_count
            for rid in counts_n:
                d = acc.setdefault(rid, dict(empty))
                d["tot_night"] += counts_n.get(rid, 0)
                d["tot_eve"] += counts_e.get(rid, 0)
                d["tot_comp"] += counts_c.get(rid, 0)
                d["tot_s4"] += counts_s4.get(rid, 0)
                d["tot_off"] += counts_o.get(rid, 0)
                d["cycles"] += 1
        return acc
