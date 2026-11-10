#!/usr/bin/env python3
"""Stage 1 of the pack pipeline: read a live Minecraft instance and write pack/manifest.json.

Every mod / resourcepack / shaderpack in the instance is hashed and looked up on
Modrinth by hash. What Modrinth knows becomes a download URL in the manifest; what
it does not know is either one of our own mods (built from a repo of ours) or an
unmatched file that needs a decision. Nothing is uploaded and nothing third-party
is redistributed.

Run this on the machine you play on, whenever your mod folder changes:

    python tools/sync_instance.py

Then commit pack/manifest.json. Stage 2 (tools/build_mrpack.py, or the
build-pack workflow) turns that manifest into a .mrpack without needing your disk.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.modrinth.com/v2"
# Generic client name on purpose: no personal details go to Modrinth.
UA = "goh-pack/0.1 (modpack build script)"
ROOT = Path(__file__).resolve().parent.parent


def api(path: str, payload: dict | None = None) -> object:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{API}{path}",
        data=data,
        headers={"User-Agent": UA, **({"Content-Type": "application/json"} if data else {})},
        method="POST" if data else "GET",
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            raise
        except urllib.error.URLError:
            if attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    raise RuntimeError("unreachable")


def hashes(path: Path) -> tuple[str, str, int]:
    sha1, sha512, size = hashlib.sha1(), hashlib.sha512(), 0
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            sha1.update(chunk)
            sha512.update(chunk)
            size += len(chunk)
    return sha1.hexdigest(), sha512.hexdigest(), size


def collect(instance: Path, scan: dict[str, list[str]], exclude: list[str]) -> list[dict]:
    found = []
    for folder, suffixes in scan.items():
        d = instance / folder
        if not d.is_dir():
            print(f"  ! {folder}/ not found in the instance, skipped")
            continue
        for f in sorted(d.iterdir()):
            if not f.is_file() or f.suffix.lower() not in suffixes:
                continue  # skips .disabled, .txt, directories
            rel = f"{folder}/{f.name}"
            if any(fnmatch.fnmatch(f.name, pat) for pat in exclude):
                print(f"  - excluded: {rel}")
                continue
            sha1, sha512, size = hashes(f)
            found.append({"path": rel, "file": f, "sha1": sha1, "sha512": sha512, "size": size})
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instance", help="override the instance path from pack.json")
    ap.add_argument("--pack", default=str(ROOT / "pack" / "pack.json"))
    ap.add_argument("--out", default=str(ROOT / "pack" / "manifest.json"))
    args = ap.parse_args()

    pack = json.loads(Path(args.pack).read_text(encoding="utf-8"))
    instance = Path(args.instance or pack["instance"])
    if not instance.is_dir():
        print(f"instance not found: {instance}", file=sys.stderr)
        return 1

    print(f"reading {instance}")
    files = collect(instance, pack.get("scan", {}), pack.get("exclude", []))
    print(f"  {len(files)} files hashed")

    # Modrinth resolves up to a few hundred hashes per call; chunk anyway.
    by_hash: dict[str, dict] = {}
    pool = [f["sha1"] for f in files]
    for i in range(0, len(pool), 100):
        by_hash.update(api("/version_files", {"hashes": pool[i : i + 100], "algorithm": "sha1"}))
    print(f"  {len(by_hash)} of {len(files)} known to Modrinth")

    # One extra call tells us which side each project belongs on.
    sides: dict[str, dict] = {}
    pids = sorted({v["project_id"] for v in by_hash.values()})
    for i in range(0, len(pids), 100):
        for p in api("/projects?ids=" + urllib.parse.quote(json.dumps(pids[i : i + 100], separators=(",", ":")))):
            sides[p["id"]] = {
                "client": "required" if p.get("client_side") != "unsupported" else "unsupported",
                "server": "required" if p.get("server_side") != "unsupported" else "unsupported",
            }

    remote, own, unmatched = [], [], []
    for f in files:
        ver = by_hash.get(f["sha1"])
        if ver:
            # Pick the file in that version whose hash is the one we have.
            mf = next((x for x in ver["files"] if x["hashes"].get("sha1") == f["sha1"]), None)
            if not mf:
                unmatched.append({k: f[k] for k in ("path", "sha1", "sha512", "size")})
                continue
            remote.append({
                "path": f["path"],
                "project_id": ver["project_id"],
                "version_id": ver["id"],
                "version_number": ver.get("version_number"),
                "url": mf["url"],
                "sha1": f["sha1"],
                "sha512": f["sha512"],
                "size": f["size"],
                "env": sides.get(ver["project_id"], {"client": "required", "server": "required"}),
            })
            continue

        mine = next((o for o in pack.get("own", []) if f["file"].name.startswith(o["prefix"])), None)
        if mine:
            stem = f["file"].stem[len(mine["prefix"]):]
            own.append({
                "path": f["path"],
                "repo": mine["repo"],
                "version": stem,
                "sha1": f["sha1"],
                "sha512": f["sha512"],
                "size": f["size"],
                "env": mine.get("env", {"client": "required", "server": "required"}),
            })
        else:
            unmatched.append({k: f[k] for k in ("path", "sha1", "sha512", "size")})

    manifest = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pack_version": pack["version"],
        "dependencies": pack["dependencies"],
        "remote": remote,
        "own": own,
        "unmatched": unmatched,
    }
    out = Path(args.out)
    out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"\nwrote {out}")
    print(f"  {len(remote):3} from Modrinth  (downloaded by each player's launcher)")
    print(f"  {len(own):3} ours           (bundled into the pack)")
    for o in own:
        print(f"        {o['path']}  ->  {o['repo']} @ {o['version']}")
    if unmatched:
        print(f"  {len(unmatched):3} UNMATCHED      (decide per file, see README)")
        for u in unmatched:
            print(f"        {u['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
