"""Push the working tree to GitHub over the REST API (api.github.com).

    git add -A
    python scripts/push_via_api.py -m "message"

A fallback for networks where github.com:443 is unreachable but api.github.com
works (common behind restrictive networks). It uploads the *index* content, so
the resulting tree is byte-identical to what `git push` would have sent.

Because the commit is created by the API, its sha differs from the local one.
Content is compared, not shas, so repeated runs stay idempotent. When the
network recovers, reconcile once with:  git fetch origin && git reset --hard origin/main
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

API = "https://api.github.com"
RETRIES = 6


def run(*args: str) -> bytes:
    p = subprocess.run(args, capture_output=True)
    if p.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(args)}\n{p.stderr.decode('utf-8', 'replace')}")
    return p.stdout


def token() -> str:
    out = subprocess.run(["gh", "auth", "token"], capture_output=True)
    if out.returncode != 0:
        raise SystemExit("gh is not authenticated (gh auth login)")
    return out.stdout.decode().strip()


class Api:
    def __init__(self, tok: str, repo: str) -> None:
        self.tok = tok
        self.repo = repo

    def call(self, method: str, path: str, payload: dict | None = None):
        url = f"{API}/repos/{self.repo}/{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        last = None
        for attempt in range(1, RETRIES + 1):
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": f"Bearer {self.tok}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "shift-scheduler-push",
                "X-GitHub-Api-Version": "2022-11-28",
            })
            if data is not None:
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=45) as r:
                    return json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                if e.code in (500, 502, 503, 504, 429) and attempt < RETRIES:
                    last = f"HTTP {e.code}: {body[:200]}"
                else:
                    raise SystemExit(f"{method} {path} -> HTTP {e.code}: {body[:400]}")
            except Exception as e:  # noqa: BLE001 - network flakiness is the norm here
                last = f"{type(e).__name__}: {e}"
            time.sleep(min(2 ** attempt, 20))
        raise SystemExit(f"{method} {path} failed after {RETRIES} attempts: {last}")


def index_entries() -> dict[str, str]:
    """path -> blob sha, as git would store them (filters/CRLF already applied)."""
    out = run("git", "ls-files", "-s", "-z").decode("utf-8", "replace")
    entries = {}
    for rec in out.split("\0"):
        if not rec:
            continue
        meta, path = rec.split("\t", 1)
        _mode, sha, _stage = meta.split()
        entries[path] = sha
    return entries


def remote_tree(api: Api, head: dict | None) -> dict[str, str]:
    if not head:
        return {}
    commit = api.call("GET", f"git/commits/{head['sha']}")
    tree = api.call("GET", f"git/trees/{commit['tree']['sha']}?recursive=1")
    return {e["path"]: e["sha"] for e in tree.get("tree", []) if e["type"] == "blob"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-m", "--message", help="commit message (default: local HEAD message)")
    ap.add_argument("--repo", help="owner/name (default: origin remote)")
    ap.add_argument("--ref", default="main")
    args = ap.parse_args()

    repo = args.repo
    if not repo:
        rem = run("git", "remote", "get-url", "origin").decode().strip()
        repo = rem.rstrip("/").removesuffix(".git").split("github.com/")[-1]
    if repo.startswith("git@"):
        repo = repo.split("github.com:")[-1]

    message = args.message
    if not message:
        try:
            message = run("git", "log", "-1", "--format=%B").decode("utf-8", "replace").strip()
        except SystemExit:
            message = "update"

    api = Api(token(), repo)

    # current remote head (may not exist on an empty repo)
    head = None
    p = subprocess.run(["gh", "api", f"repos/{repo}/git/ref/heads/{args.ref}"],
                       capture_output=True)
    if p.returncode == 0:
        head = json.loads(p.stdout)["object"]

    local = index_entries()
    remote = remote_tree(api, head)

    added = [f for f in local if f not in remote]
    changed = [f for f in local if f in remote and local[f] != remote[f]]
    deleted = [f for f in remote if f not in local]
    if not (added or changed or deleted):
        print(f"up to date with {repo}@{args.ref} ({len(local)} files)")
        return 0
    print(f"{repo}@{args.ref}: +{len(added)} ~{len(changed)} -{len(deleted)}")

    entries = []
    for path in sorted(added + changed):
        blob = run("git", "cat-file", "blob", local[path])
        sha = api.call("POST", "git/blobs",
                       {"content": base64.b64encode(blob).decode(), "encoding": "base64"})["sha"]
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": sha})
    for path in sorted(deleted):
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})

    tree_payload: dict = {"tree": entries}
    if head:
        commit = api.call("GET", f"git/commits/{head['sha']}")
        tree_payload["base_tree"] = commit["tree"]["sha"]
    tree = api.call("POST", "git/trees", tree_payload)

    commit = api.call("POST", "git/commits",
                      {"message": message, "tree": tree["sha"],
                       "parents": [head["sha"]] if head else []})
    if head:
        api.call("PATCH", f"git/refs/heads/{args.ref}", {"sha": commit["sha"]})
    else:
        api.call("POST", "git/refs",
                 {"ref": f"refs/heads/{args.ref}", "sha": commit["sha"]})
    print(f"pushed {commit['sha'][:12]} -> {repo}@{args.ref}")
    print("note: the remote commit sha differs from the local one (same content); "
          "reconcile with `git fetch origin && git reset --hard origin/main` when the "
          "network allows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
