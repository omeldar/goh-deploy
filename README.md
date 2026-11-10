# goh-deploy

How GoH reaches a machine. Two things live here:

- `mods/`, `config/` — what gets staged onto the **server**.
- `pack/`, `tools/` — the **client modpack**, built from the instance Eldar actually plays.

## The modpack, in two stages

The jars are on your PC; CI has no access to them. So the part that needs your disk
is separated from the part that builds the file everyone downloads.

### Stage 1 — sync, on your machine

```sh
python tools/sync_instance.py
```

It reads the instance named in `pack/pack.json`, hashes every mod, resourcepack and
shaderpack, and asks Modrinth which version each hash belongs to. The result is
`pack/manifest.json`, which sorts every file into one of three piles:

| pile | what happens to it |
| --- | --- |
| **remote** | On Modrinth. The manifest stores its CDN URL and hashes; each player's launcher downloads it. Nothing of anyone else's is redistributed. |
| **ours** | One of our mods (matched by the prefixes in `pack.json`). Bundled into the pack. |
| **unmatched** | Modrinth does not know it and it isn't ours. Needs a decision — see below. |

`.disabled` files are skipped, so disabling a mod in the launcher takes it out of the
pack. Commit `pack/manifest.json` when you're happy with it: that file *is* the pack
definition, and a diff on it shows exactly what changed between pack versions.

As of the last sync: **82 from Modrinth, 4 ours, 0 unmatched.**

### Stage 2 — build, anywhere

```sh
python tools/build_mrpack.py --own-from local     # uses the jars in your instance
python tools/build_mrpack.py --own-from github    # pulls our jars from their releases
```

Out comes `dist/GoH-<version>.mrpack`. The same script runs in the **Build modpack**
workflow (Actions → Run workflow), which is manual-only: pick a version, pick where
our own mods come from, and optionally attach the result to a release.

`--own-from github` looks for the release tagged `v<version>` in each mod's repo and
fails loudly if it isn't there, rather than quietly shipping a different build than the
one you tested. So cut the release first, or build with `--own-from local`.

## Unmatched files

If stage 1 reports unmatched files, the build refuses to run until you decide:

- **A mod of ours that `pack.json` doesn't know** → add its prefix to `own`.
- **Something you want in the pack anyway** → `--bundle-unmatched` puts it in the zip.
  Fine for our own work; for someone else's mod it means redistributing it, so check
  its licence first.
- **Something that shouldn't be in the pack** → disable or delete it in the instance
  and sync again.

## Why no config in the pack

`config/` from the instance is deliberately not shipped. Several of those files hold
per-machine paths and bridge tokens for Lumi and Autopilot, and the GoH hub mod already
walks a new player through first-run setup. If a specific config ever needs to ship, add
that one path to `overrides` in `pack.json` — not the whole folder.

## How players install it

Download the `.mrpack` and open it with the Modrinth App or Prism Launcher. It creates
the instance, pulls the 82 Modrinth files itself, and drops our four mods in place.

Updating means importing the new `.mrpack`. If that friction gets annoying, the next step
is **packwiz**: a hosted index plus a small bootstrap jar gives real one-click updates and
can still export a `.mrpack` from the same manifest.
