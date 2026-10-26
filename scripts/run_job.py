"""Run one queued job (the JSON payload carried by a GitHub issue).

    python scripts/run_job.py --body-env ISSUE_BODY \
        --out data/last_result.json --comment build/job-comment.md

The payload is what static/app.js puts in the issue body:

    ```json
    {"v":1,"op":"repair","jid":"m1a2b3","payload":{...}}
    ```

Every endpoint of app.py is a plain function taking a payload, so the job
runs the exact same code (and the exact same validation) as a browser click.

Always exits 0 — the workflow commits the result and turns the run red at the
end, so the failure still reaches both the issue and the hosted page.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import traceback
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):  # keep the issue comment UTF-8 everywhere
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import app as appmod  # noqa: E402
from fastapi import HTTPException  # noqa: E402

OPS = {
    "repair": appmod.repair_cell,
    "generate": appmod.generate_cycle,
    "restore": appmod.restore_version,
    "resources": appmod.set_resources,
    "settings": appmod.set_settings,
    "startstate": appmod.set_start_state,
}
FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_job(text: str) -> dict:
    if not text or not text.strip():
        raise ValueError("the issue body is empty")
    m = FENCE.search(text)
    raw = m.group(1) if m else text
    job = json.loads(raw)
    if not isinstance(job, dict):
        raise ValueError("payload is not a JSON object")
    op = str(job.get("op") or "").strip()
    if op not in OPS:
        raise ValueError(f"unknown op {op!r} — expected one of {', '.join(sorted(OPS))}")
    payload = job.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    return {"op": op, "payload": payload, "jid": str(job.get("jid") or "unknown")}


def comment_markdown(out: dict) -> str:
    if not out["ok"]:
        return (f"**`{out['op']}` failed** — nothing was changed.\n\n"
                f"```\n{out.get('detail', 'unknown error')}\n```\n\n"
                f"_(queued as {out['jid']})_")
    label = out.get("label") or out["op"]
    lines = [f"**`{out['op']}` applied** — {label}"]
    changes = out.get("changes") or []
    if changes:
        lines.append(f"\n{len(changes)} cell(s) changed:")
        for c in changes[:20]:
            who = c.get("name", c.get("resource_id"))
            lines.append(f"- {who} · {c.get('date', c.get('day'))} · "
                         f"{c.get('from')} → **{c.get('to')}**")
        if len(changes) > 20:
            lines.append(f"- … and {len(changes) - 20} more")
    for w in (out.get("warnings") or []):
        lines.append(f"- ⚠️ {w.get('message', w)}")
    lines.append(f"\n_Reload the app to see it. ({out['jid']})_")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--body", help="literal issue body")
    ap.add_argument("--body-env", help="name of an env var holding the issue body")
    ap.add_argument("--out", default=str(BASE / "data" / "last_result.json"))
    ap.add_argument("--comment", help="path to write the issue comment markdown")
    args = ap.parse_args()

    if args.body is not None:
        text = args.body
    elif args.body_env:
        text = os.environ.get(args.body_env, "")
    else:
        ap.error("need --body or --body-env")

    try:
        job = parse_job(text)
    except Exception as e:  # noqa: BLE001 - any parse problem is a user-visible error
        job = None
        failure = f"could not read the job: {e}"

    out: dict
    if job is None:
        out = {"ok": False, "op": "unknown", "jid": "unknown", "detail": failure}
    else:
        prev = appmod.store.current_version()
        try:
            result = OPS[job["op"]](job["payload"])
            out = {"ok": True, "op": job["op"], "jid": job["jid"], **(result or {})}
            out["previous_version_id"] = prev["id"] if prev else None
            ver = out.get("version") or {}
            out["label"] = ver.get("label") or job["op"]
        except HTTPException as e:
            out = {"ok": False, "op": job["op"], "jid": job["jid"],
                   "detail": str(e.detail)}
        except Exception:  # noqa: BLE001
            out = {"ok": False, "op": job["op"], "jid": job["jid"],
                   "detail": "internal error:\n" + traceback.format_exc(limit=8)}

    out["at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    md = comment_markdown(out)
    if args.comment:
        cpath = Path(args.comment)
        cpath.parent.mkdir(parents=True, exist_ok=True)
        cpath.write_text(md, encoding="utf-8")

    gha = os.environ.get("GITHUB_OUTPUT")
    if gha:
        with open(gha, "a", encoding="utf-8") as f:
            f.write(f"ok={'true' if out['ok'] else 'false'}\n")
            f.write(f"op={out.get('op', 'unknown')}\n")
            f.write(f"jid={out.get('jid', 'unknown')}\n")

    print(md)
    print(f"result -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
