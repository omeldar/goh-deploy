#!/usr/bin/env python3
"""Stage 2 of the pack pipeline: turn pack/manifest.json into a .mrpack.

Third-party mods go in as Modrinth download URLs, so each player's launcher
fetches them itself and nothing of anyone else's is redistributed. Our own mods
are bundled into the zip under overrides/, taken either from a local folder or
from their GitHub release.

    python tools/build_mrpack.py --own-from local --jars "<instance>/mods"
    python tools/build_mrpack.py --own-from github          # what CI does
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

UA = "goh-pack/0.1 (modpack build script)"
ROOT = Path(__file__).resolve().parent.parent


def fetch(url: str, token: str | None = None) -> bytes:
    headers = {"User-Agent": UA, "Accept": "application/octet-stream"}
    if token and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as r:
        return r.read()


def github_jar(repo: str, version: str, token: str | None) -> bytes:
    """Download the single .jar asset of the release whose tag matches `version`."""
    for tag in (f"v{version}", version):
        try:
            rel = json.loads(fetch(f"https://api.github.com/repos/{repo}/releases/tags/{tag}", token))
            break
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            rel = None
    if rel is None:
        raise SystemExit(
            f"no release tagged v{version} in {repo}.\n"
            f"  Either cut that release, or build with --own-from local."
        )
    assets = [a for a in rel.get("assets", []) if a["name"].endswith(".jar")
              and not a["name"].endswith(("-sources.jar", "-dev.jar"))]
    if not assets:
        raise SystemExit(f"release {tag} of {repo} has no .jar asset")
    return fetch(assets[0]["url"], token)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pack", default=str(ROOT / "pack" / "pack.json"))
    ap.add_argument("--manifest", default=str(ROOT / "pack" / "manifest.json"))
    ap.add_argument("--out-dir", default=str(ROOT / "dist"))
    ap.add_argument("--own-from", choices=["local", "github"], default="local")
    ap.add_argument("--jars", help="folder holding our own jars when --own-from local "
                                   "(default: the instance's mods folder from pack.json)")
    ap.add_argument("--version", help="override the pack version in the output")
    ap.add_argument("--bundle-unmatched", action="store_true",
                    help="put files Modrinth does not know into the zip instead of failing")
    args = ap.parse_args()

    pack = json.loads(Path(args.pack).read_text(encoding="utf-8"))
    man = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    version = args.version or man.get("pack_version") or pack["version"]
    token = os.environ.get("GITHUB_TOKEN")

    files = [{
        "path": r["path"],
        "hashes": {"sha1": r["sha1"], "sha512": r["sha512"]},
        "env": r["env"],
        "downloads": [r["url"]],
        "fileSize": r["size"],
    } for r in man["remote"]]

    # Our own mods, and anything we chose to bundle, ride along inside the zip.
    bundled: list[tuple[str, bytes]] = []
    jar_dir = Path(args.jars) if args.jars else Path(pack["instance"]) / "mods"
    for o in man["own"]:
        name = Path(o["path"]).name
        if args.own_from == "github":
            print(f"  github  {o['repo']} @ {o['version']}")
            blob = github_jar(o["repo"], o["version"], token)
        else:
            src = jar_dir / name
            if not src.is_file():
                raise SystemExit(f"missing jar: {src}\n  Re-run tools/sync_instance.py, or pass --jars.")
            print(f"  local   {src.name}")
            blob = src.read_bytes()
        got = hashlib.sha1(blob).hexdigest()
        if args.own_from == "local" and got != o["sha1"]:
            raise SystemExit(f"{name} changed since the last sync; re-run tools/sync_instance.py")
        if args.own_from == "github" and got != o["sha1"]:
            print(f"    note: release jar differs from the synced one ({got[:8]} vs {o['sha1'][:8]})")
        bundled.append((f"overrides/{o['path']}", blob))

    if man.get("unmatched"):
        if not args.bundle_unmatched:
            print("\nthese files are not on Modrinth and not ours:", file=sys.stderr)
            for u in man["unmatched"]:
                print(f"  {u['path']}", file=sys.stderr)
            print("Decide per file (see README), then re-run with --bundle-unmatched "
                  "or drop them from the instance.", file=sys.stderr)
            return 1
        for u in man["unmatched"]:
            src = Path(pack["instance"]) / u["path"]
            if not src.is_file():
                raise SystemExit(f"cannot bundle {u['path']}: not found at {src}")
            print(f"  bundled {u['path']}")
            bundled.append((f"overrides/{u['path']}", src.read_bytes()))

    index = {
        "formatVersion": 1,
        "game": "minecraft",
        "versionId": version,
        "name": pack["name"],
        "summary": pack.get("summary", ""),
        "files": files,
        "dependencies": man["dependencies"],
    }

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{pack['name']}-{version}.mrpack"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("modrinth.index.json", json.dumps(index, indent=2))
        for name, blob in bundled:
            z.writestr(name, blob)
        for extra in pack.get("overrides", []):
            root = Path(pack["instance"]) / extra
            for f in sorted(root.rglob("*")) if root.is_dir() else []:
                if f.is_file():
                    z.write(f, f"overrides/{extra}/{f.relative_to(root).as_posix()}")

    print(f"\n{out}")
    print(f"  {len(files)} fetched by the launcher, {len(bundled)} bundled, "
          f"{out.stat().st_size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
