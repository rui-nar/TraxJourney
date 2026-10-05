#!/usr/bin/env python
"""Install the published rail extracts into RAIL_DATA_DIR (issue #345, delivery).

Phase 1 builds rail-only country extracts in CI and publishes them as assets on
a prerelease tagged ``rail-data-<YYYY-MM-DD>``. Phase 2 turns one of those into
the per-region SQLite store ``src/rail/store.py`` reads. Phase 3 reads the
stores when ``RAIL_SOURCE=local``. Nothing put the data on the box: this does.

Run it on the box, by hand or from whatever schedules it, never at container
boot — see docs/DEPLOYMENT_VPS.md, "Rail data".

    python scripts/fetch_rail_data.py --dest /app/data/rail

**It fetches the extract and builds the store here rather than downloading a
prebuilt store**, because the release holds extracts and only extracts. The
build is affordable — Germany, the largest region, is ~12 s and ~200 MB peak —
and the extract is a fraction of the size of the store it produces — Germany
10 MB against 58 MB, a sixth; Luxembourg, measured on the real published
asset, 0.33 MB against 1.36 MB, a quarter — so building locally moves *less*
over the network, not more. ``osmium`` is already in requirements.txt and
therefore already in the image.

Four properties, in the order they matter:

1. **A store is moved into place with :func:`os.replace` and never written
   where a reader can see it half-written.** ``src/services/rail_source.py``
   guards *opening* a store, not querying one, so a ``sqlite3.DatabaseError``
   raised on a connection a worker already holds escapes into the RQ job
   (issue #352, finding 2). Overwriting a file in place while a worker has it
   open is the one way to cause that. A rename leaves the old inode alive for
   every open handle, so the failure cannot happen — which is why the work
   directory lives *inside* the destination, on the same filesystem: a rename
   across filesystems is a copy, and a copy is not atomic.
2. **Every asset is checked against the manifest's sha256 and byte count
   before it is built from**, and a region that fails leaves whatever is
   already installed exactly where it is.
3. **Re-running converges.** A region whose store was installed from this
   manifest's checksum *by this store schema* is skipped, so a run interrupted
   after 30 of 49 regions costs 19 regions the second time, not 49 — while a
   schema bump, which leaves the extract byte-identical, rebuilds all 49
   instead of skipping them as up to date.
4. **Bounded.** One region's extract plus the store built from it, and the
   extract is deleted the moment the store exists. Nothing accumulates: the
   VPS has 40 GB for two whole stacks.
5. **One refresh at a time.** Two overlapping runs are the one way this step
   can publish a corrupt store, and the sidecar would then make it permanent
   — see :func:`_staging`, which holds the lock and hands out this run's own
   staging directory.

The record of what is installed is a sidecar ``<store>.sha256`` beside each
store, written *after* the rename, holding ``<asset digest> <store schema>``.
The store itself cannot carry it — the builder's ``meta`` records the source
file's name and date but not its digest, and the builder is not this step's to
change. Both fields are part of the key: see :func:`installed_build` for why a
digest alone turns a schema bump into a refresh that silently does not happen.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import secrets
import shutil
import sys
from pathlib import Path
from typing import Callable, Iterator, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.rail.builder import build_store  # noqa: E402 — after the sys.path fix
from src.rail.store import (  # noqa: E402
    DEFAULT_LAYER,
    LAYERS,
    SCHEMA_VERSION,
    store_filename,
)

# The repository the extracts are published from. Public, so the releases API
# answers unauthenticated (verified: GET /repos/<repo>/releases returns 200 with
# no Authorization header) and asset URLs need no token either. This step
# therefore takes no credentials at all — one fewer secret on the box, and one
# fewer reason a data refresh can fail.
DEFAULT_REPO = "rui-nar/TraxJourney"
RELEASES_URL = "https://api.github.com/repos/{repo}/releases?per_page=100"
# An explicit --tag is fetched by name, never looked for in that list: the list
# is one page of 100 and this repository publishes ~15 releases a month, so the
# rollback target — last month's rail-data release — drops off page one within
# weeks and rollback would die with "no rail data release tagged ...".
RELEASE_BY_TAG_URL = "https://api.github.com/repos/{repo}/releases/tags/{tag}"
TAG_PREFIX = "rail-data-"

MANIFEST_NAME = "manifest.json"
# The release manifest schemas this version installs from; anything else is
# refused outright. A manifest read with the wrong shape would install stores
# under a coverage claim nobody has checked, which is the failure that looks
# exactly like success. Schema 3 gives each entry a `layer` (rail, ferry, bus);
# a schema 2 entry, which has none, is rail. Both are accepted so that this code
# can ship before the first schema 3 release, and keep installing schema 2
# releases — every rollback target until then — after it.
RELEASE_SCHEMAS = (2, 3)
# What the *installed* manifest is written as: still 2, whatever the release
# was. The age check reads it against src/services/rail_source.MANIFEST_SCHEMA,
# which says why; its entries may carry `layer`, which every reader on the box
# reads as rail when it is missing.
MANIFEST_SCHEMA = 2
STATUS_OK = "ok"
STATUS_EMPTY = "empty"

# Inside the destination on purpose: os.replace is only atomic within one
# filesystem, and this is what guarantees it is one filesystem.
WORK_DIRNAME = ".incoming"
LOCK_NAME = ".lock"
SHA_SUFFIX = ".sha256"
PART_SUFFIX = ".part"

_CHUNK = 1 << 20


class RailDataError(Exception):
    """The refresh cannot proceed at all — no release, no manifest, bad schema."""


def _log(message: str) -> None:
    print(message, flush=True)


# ---------------------------------------------------------------------------
# The release
# ---------------------------------------------------------------------------

def _requests_get() -> Callable:
    import requests  # noqa: PLC0415 — only this step talks to the network

    return requests.get


def pick_release(releases: list[dict]) -> dict:
    """The newest rail-data release in *releases*.

    Tags are ``rail-data-<YYYY-MM-DD>``, so newest is the lexical maximum. Only
    those are considered — the repository's own app releases share the list.
    """
    candidates = [r for r in releases
                  if str(r.get("tag_name", "")).startswith(TAG_PREFIX)
                  and not r.get("draft")]
    if not candidates:
        raise RailDataError(
            f"no {TAG_PREFIX}* release published yet — run the rail-extract "
            f"workflow first (.github/workflows/rail-extract.yml)")
    return max(candidates, key=lambda r: r["tag_name"])


def fetch_release(get: Callable, repo: str, tag: Optional[str]) -> dict:
    """The release to install from: *tag* by name, or the newest published one.

    Asking GitHub for the tag directly is what keeps rollback working past the
    hundredth release on this repository — see :data:`RELEASE_BY_TAG_URL`.
    """
    if tag is None:
        response = get(RELEASES_URL.format(repo=repo), timeout=60)
        response.raise_for_status()
        return pick_release(json.loads(response.content))
    response = get(RELEASE_BY_TAG_URL.format(repo=repo, tag=tag), timeout=60)
    if response.status_code == 404:
        raise RailDataError(f"no release tagged {tag}")
    response.raise_for_status()
    return json.loads(response.content)


def asset_urls(release: dict) -> dict[str, str]:
    return {a["name"]: a["browser_download_url"] for a in release.get("assets", [])}


def read_manifest(get: Callable, url: str) -> dict:
    """Download and validate the release's manifest.json."""
    response = get(url, timeout=60)
    response.raise_for_status()
    try:
        manifest = json.loads(response.content)
    except ValueError as exc:
        raise RailDataError(f"{url}: manifest is not JSON ({exc})") from exc
    schema = manifest.get("schema")
    if schema not in RELEASE_SCHEMAS:
        raise RailDataError(
            f"{url}: manifest schema {schema!r}, expected one of "
            f"{', '.join(str(s) for s in RELEASE_SCHEMAS)} — "
            f"refusing to install from a manifest this version does not know")
    if not isinstance(manifest.get("regions"), list):
        raise RailDataError(f"{url}: manifest has no regions list")
    return manifest


# ---------------------------------------------------------------------------
# What is already installed
# ---------------------------------------------------------------------------

def entry_layer(entry: dict) -> str:
    """The entry's layer. A schema 2 entry has none, and is rail."""
    return entry.get("layer", DEFAULT_LAYER)


def _label(region: str, layer: str) -> str:
    """How a log line names a (region, layer): rail as it always has."""
    return f"[{region}]" if layer == DEFAULT_LAYER else f"[{region} {layer}]"


def _sidecar(dest: Path, region: str, layer: str = DEFAULT_LAYER) -> Path:
    return dest / (store_filename(region, layer) + SHA_SUFFIX)


def installed_build(dest: Path, region: str,
                    layer: str = DEFAULT_LAYER) -> Optional[tuple[str, int]]:
    """(asset digest, store schema) the installed *layer* store was built from, or None.

    One store and one sidecar per (region, layer); rail's are named as they
    were before layers existed, so a rail sidecar on the box is found as is.

    None whenever anything is missing or unreadable, so the region is simply
    rebuilt — this record exists to save work, never to authorise skipping it.

    The schema is half of the record because the extract is not a store's only
    input: the *builder* is the other, and a schema bump changes what the store
    holds while the asset it was built from stays byte-identical. Keyed on the
    digest alone, the first run after such a bump skips every region as up to
    date and the box keeps stores the reader can only half use — a republish
    that silently does not happen. Kept as a second field so that an old
    sidecar, holding one token, reads as "schema unknown" and rebuilds, which
    is exactly what that first run must do.
    """
    if not (dest / store_filename(region, layer)).is_file():
        return None
    try:
        parts = _sidecar(dest, region, layer).read_text(encoding="utf-8").split()
    except OSError:
        return None
    if len(parts) != 2 or not parts[0]:
        return None
    try:
        return parts[0], int(parts[1])
    except ValueError:
        return None


def _record_build(dest: Path, work: Path, region: str, layer: str, digest: str) -> None:
    # Staged and renamed like everything else here. A torn sidecar would cost
    # only a needless rebuild, but "every file appears by atomic rename" is a
    # documented contract and an exception nobody can see is how contracts rot.
    staged = work / (store_filename(region, layer) + SHA_SUFFIX + PART_SUFFIX)
    staged.write_text(f"{digest} {SCHEMA_VERSION}\n", encoding="utf-8")
    os.replace(staged, _sidecar(dest, region, layer))


# ---------------------------------------------------------------------------
# One region
# ---------------------------------------------------------------------------

def _download(get: Callable, url: str, dest: Path) -> tuple[str, int]:
    """Stream *url* to *dest*, returning its (sha256, bytes).

    Streamed and digested as it lands: the caller compares both against the
    manifest before anything is built from the file.
    """
    digest = hashlib.sha256()
    size = 0
    with get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        with dest.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=_CHUNK):
                if not chunk:
                    continue
                digest.update(chunk)
                size += len(chunk)
                handle.write(chunk)
    return digest.hexdigest(), size


def install_region(get: Callable, entry: dict, url: str, dest: Path, work: Path) -> None:
    """Fetch one (region, layer)'s extract, build its store and move it into place.

    Raises on any failure, having left the previously installed store — if any
    — untouched. Nothing is written into *dest* before the checksum matches and
    the store is fully built.
    """
    region = entry["region"]
    layer = entry_layer(entry)
    # *work* is this run's own directory (see _staging), so these names cannot
    # collide with a concurrent run's. basename because the file name comes
    # from the manifest: a GitHub asset name cannot contain a slash, but this
    # is the one place a manifest value is used as a path.
    pbf = work / os.path.basename(entry["file"])
    staged = work / (store_filename(region, layer) + PART_SUFFIX)
    try:
        digest, size = _download(get, url, pbf)
        if size != entry["bytes"] or digest != entry["sha256"]:
            raise RailDataError(
                f"{entry['file']}: downloaded {size} bytes sha256 {digest}, "
                f"manifest says {entry['bytes']} bytes sha256 {entry['sha256']}")
        build_store(pbf, staged, region=region,
                    source_date=entry.get("source_date", ""), layer=layer)
        pbf.unlink()
        # The whole design in one line: the reader's open handle keeps the old
        # inode, so a refresh cannot corrupt a query already in flight.
        os.replace(staged, dest / store_filename(region, layer))
        # After the rename, never before: a sidecar recording a digest whose
        # store did not make it into place would make every later run skip the
        # region as "up to date" and the stale data would never be replaced.
        _record_build(dest, work, region, layer, digest)
    finally:
        for leftover in (pbf, staged):
            try:
                leftover.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# The refresh
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _staging(dest: Path) -> Iterator[Optional[Path]]:
    """Yield this run's private staging directory, or None if a run is on.

    Two refreshes overlapping is the one way this step can publish a broken
    store: ``build_store`` removes its output and rebuilds it, so a second run
    building on the same staged path while the first renames it publishes a
    half-written database — and the sidecar written next records the *correct*
    digest, so every later run then skips the region as up to date and the
    corruption is permanent. Two things prevent it, and either alone would:

    * an exclusive ``flock`` on ``.incoming/.lock``, so only one refresh runs
      at a time. A second run does nothing and exits 0 rather than waiting:
      the directory is being brought up to date by the run that holds the lock,
      and a blocked ``docker compose run`` looks like a hang. ``flock`` is
      POSIX; on Windows (dev machines only, never production) there is none;
    * a staging directory unique to this run, so even unlocked, no two runs
      share a path.

    Leftovers from a run that was killed outright — the one failure the
    per-region cleanup cannot handle, an OOM kill being the realistic case —
    are removed here, under the lock, so ``.incoming/`` holds nothing but the
    lock file between runs.
    """
    root = dest / WORK_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    with (root / LOCK_NAME).open("w") as handle:
        if not _take_lock(handle):
            yield None
            return
        for leftover in root.iterdir():
            if leftover.name == LOCK_NAME:
                continue
            if leftover.is_dir():
                shutil.rmtree(leftover, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    leftover.unlink()
        work = root / f"run-{os.getpid()}-{secrets.token_hex(4)}"
        work.mkdir()
        try:
            yield work
        finally:
            shutil.rmtree(work, ignore_errors=True)


def _take_lock(handle) -> bool:
    """Take the exclusive lock on *handle*, or report that someone else has it."""
    try:
        import fcntl  # noqa: PLC0415 — POSIX only, absent on dev machines
    except ImportError:
        return True
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _existing_manifest(dest: Path) -> dict:
    try:
        with (dest / MANIFEST_NAME).open("r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, ValueError):
        return {}
    return manifest if isinstance(manifest, dict) else {}


# Marks on an installed-manifest entry the release omitted: kept for one
# release (`carried`), and which release omitted it (`omitted_by`) — so a re-run
# of that same release, the normal retry after a refused region, keeps carrying
# it instead of mistaking itself for the next release and retiring it.
CARRIED = "carried"
OMITTED_BY = "omitted_by"


def _carry_omitted(dest: Path, release_manifest: dict, tag: str) -> list[dict]:
    """The installed entries to keep for (region, layer)s *release_manifest* omits.

    Says what happens to each one, because nothing else will (review finding
    R1-6): the installed manifest is rebuilt from the release, so an omitted
    entry would otherwise leave coverage — its routes back to Overpass — while
    the age check, which reads the same manifest, kept reporting fresh data.

    * **Carried, once** (R2-2): an ``ok`` entry whose store and sidecar are on
      disk stays listed, marked ``carried: true``, exactly as a region whose
      install failed keeps its entry. It still routes, and the age gauge keeps
      seeing it age rather than losing sight of it. A ferry layer that failed
      in CI one month is back the next.
    * **Retired** (R3-1): an entry already carried, which the *next* release
      omits too, leaves the manifest — what removing a region or layer from
      ``config/rail_regions.yml`` looks like from here, and it needs no manual
      step. Its store file lingers unreferenced, as a dropped region's always
      has (docs/DEPLOYMENT_VPS.md §9).
    * **Dropped** otherwise: no store on disk to keep serving, or an ``empty``
      entry, which has none by definition.
    """
    published = {(e.get("region"), entry_layer(e)) for e in release_manifest["regions"]
                 if isinstance(e, dict)}
    installed = _existing_manifest(dest).get("regions")
    previous = [e for e in (installed if isinstance(installed, list) else [])
                if isinstance(e, dict) and e.get("region")
                and entry_layer(e) in LAYERS]
    carried = []
    for entry in sorted(previous, key=lambda e: (e["region"], entry_layer(e))):
        region, layer = entry["region"], entry_layer(entry)
        if (region, layer) in published:
            continue
        label = _label(region, layer)
        again = entry.get(CARRIED) and entry.get(OMITTED_BY) == tag
        if entry.get(CARRIED) and not again:
            _log(f"WARNING: {label} was carried because "
                 f"{entry.get(OMITTED_BY) or 'the previous release'} did not "
                 f"publish it, and {tag} does not either — treated as retired: "
                 f"it leaves the installed manifest and its routes go to Overpass")
            continue
        if (entry.get("status", STATUS_OK) == STATUS_OK
                and installed_build(dest, region, layer) is not None):
            if again:
                # This release re-run — the retry after a refused region. It
                # was decided, and said, on the first run.
                carried.append(entry)
                continue
            carried.append({**entry, CARRIED: True, OMITTED_BY: tag})
            _log(f"WARNING: {label} is installed but {tag} does not publish it — "
                 f"its installed store is carried for this one release and still "
                 f"ages; if the next release omits it too, it is retired")
            continue
        _log(f"WARNING: {label} is installed but {tag} does not publish it — it "
             f"leaves the installed manifest and its routes go to Overpass")
    return carried


def _installed_manifest(dest: Path, release_manifest: dict, complete: bool,
                        carried: list[dict]) -> dict:
    """The manifest describing what *dest* actually holds.

    Entries are keyed by (region, layer), a missing ``layer`` being rail.
    They are the release's, verbatim, for every (region, layer) whose store is
    installed at that entry's checksum. One that failed this run keeps the
    entry it had, because the store on disk is still the one that entry
    describes — dropping it would send a country that is sitting right there
    back to Overpass — and loses any ``carried`` mark, because this release
    does publish it. ``empty`` entries are carried through as they are: there
    is no file to install and Phase 3 reads them as "we know there is no rail
    here". An entry for a layer this version does not know is left out.
    *carried* are the entries :func:`_carry_omitted` kept for (region, layer)s
    the release omits.

    ``generated_at`` is the release's only when every region was installed.
    A partial run keeps the old value rather than claiming a freshness the
    directory does not have — Phase 5 alerts on data age, and an optimistic
    timestamp is the one failure mode that looks like success.
    """
    previous = _existing_manifest(dest)
    kept = {(e.get("region"), entry_layer(e)): e for e in previous.get("regions", [])
            if isinstance(e, dict)}
    regions = list(carried)
    for entry in release_manifest["regions"]:
        region, layer = entry.get("region"), entry_layer(entry)
        if layer not in LAYERS:
            continue
        if entry.get("status", STATUS_OK) == STATUS_EMPTY:
            regions.append(entry)
        elif (entry.get("sha256") and installed_build(dest, region, layer)
              == (entry["sha256"], SCHEMA_VERSION)):
            regions.append(entry)
        elif (region, layer) in kept:
            regions.append({k: v for k, v in kept[(region, layer)].items()
                            if k not in (CARRIED, OMITTED_BY)})
    generated_at = release_manifest.get("generated_at", "")
    if not complete:
        generated_at = previous.get("generated_at", generated_at)
    return {
        "schema": MANIFEST_SCHEMA,
        "generated_at": generated_at,
        # Rail sorts after bus and ferry within a region, so a reader from
        # before layers — which keys this file by region alone, last entry
        # winning — lands on the rail entry after a rollback.
        "regions": sorted(regions, key=lambda e: (e.get("region", ""), entry_layer(e))),
    }


def _write_manifest(dest: Path, work: Path, manifest: dict) -> None:
    staged = work / (MANIFEST_NAME + PART_SUFFIX)
    staged.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    # Same reason as the stores: a worker re-reads this file every five minutes
    # and must never see it half-written.
    os.replace(staged, dest / MANIFEST_NAME)


def refresh(
    dest: str | os.PathLike,
    get: Callable | None = None,
    repo: str = DEFAULT_REPO,
    tag: Optional[str] = None,
) -> int:
    """Bring *dest* up to date with the published rail data.

    Returns the number of regions that failed; 0 means the directory now holds
    every region the release publishes. Raises :class:`RailDataError` when the
    release or its manifest cannot be used at all, in which case *dest* is
    untouched. Does nothing at all, and returns 0, while another refresh of the
    same directory is running.
    """
    if get is None:
        get = _requests_get()
    dest = Path(dest)

    with _staging(dest) as work:
        if work is None:
            _log(f"{dest}: another refresh is already running — this run does "
                 f"nothing. Nothing is lost: that run installs the same data.")
            return 0

        release = fetch_release(get, repo, tag)
        urls = asset_urls(release)
        if MANIFEST_NAME not in urls:
            raise RailDataError(f"{release['tag_name']} has no {MANIFEST_NAME} asset")
        manifest = read_manifest(get, urls[MANIFEST_NAME])
        _log(f"{release['tag_name']}: {len(manifest['regions'])} regions, "
             f"generated {manifest.get('generated_at', '?')}")
        carried = _carry_omitted(dest, manifest, release["tag_name"])

        installed = skipped = empty = 0
        failed: list[str] = []
        for entry in manifest["regions"]:
            region = entry.get("region", "?")
            layer = entry_layer(entry)
            if layer not in LAYERS:
                # Something newer published it. Not a refusal — the layers this
                # version does know are unaffected — and not installed either:
                # there is no store name, and no reader, for it here.
                _log(f"WARNING: [{region}] layer {layer!r} is not one this version "
                     f"knows ({', '.join(LAYERS)}) — ignored")
                continue
            label = _label(region, layer)
            status = entry.get("status", STATUS_OK)
            if status == STATUS_EMPTY:
                empty += 1
                _log(f"{label} empty — no {layer} in this region, nothing to fetch")
                continue
            if status != STATUS_OK:
                failed.append(label)
                _log(f"{label} REFUSED: unknown status {status!r}")
                continue
            digest = entry.get("sha256")
            if not digest:
                # Nothing to verify against, so nothing may be installed: an
                # entry with no digest would otherwise match an *absent*
                # sidecar and be skipped as up to date, claiming coverage with
                # no file behind it.
                failed.append(label)
                _log(f"{label} REFUSED: manifest entry has no sha256")
                continue
            if installed_build(dest, region, layer) == (digest, SCHEMA_VERSION):
                skipped += 1
                _log(f"{label} up to date ({entry.get('source_date', '?')})")
                continue
            url = urls.get(entry.get("file"))
            if url is None:
                failed.append(label)
                _log(f"{label} REFUSED: {release['tag_name']} has no asset "
                     f"{entry.get('file')!r}")
                continue
            try:
                install_region(get, entry, url, dest, work)
            except Exception as exc:  # noqa: BLE001 — one region must not stop the rest
                failed.append(label)
                _log(f"{label} REFUSED: {exc} — the installed store is unchanged")
                continue
            installed += 1
            _log(f"{label} installed {entry['file']} "
                 f"({entry['bytes']} bytes, source {entry.get('source_date', '?')})")

        _write_manifest(dest, work,
                        _installed_manifest(dest, manifest, not failed, carried))
        _log(f"{dest}: {installed} installed, {skipped} up to date, {empty} empty, "
             f"{len(failed)} refused")
        if failed:
            _log("refused: " + ", ".join(f.strip("[]") for f in failed)
                 + " — re-run to retry only these")
        return len(failed)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Install the published rail extracts into RAIL_DATA_DIR")
    ap.add_argument(
        "--dest", default=os.getenv("RAIL_DATA_DIR"),
        help="directory RAIL_SOURCE=local reads (default: $RAIL_DATA_DIR)")
    ap.add_argument("--repo", default=DEFAULT_REPO, help=argparse.SUPPRESS)
    ap.add_argument(
        "--tag", default=None,
        help=f"install a specific {TAG_PREFIX}* release instead of the newest "
             f"— this is the rollback path")
    args = ap.parse_args(argv)
    if not args.dest:
        ap.error("--dest is required when RAIL_DATA_DIR is not set")
    try:
        return 1 if refresh(args.dest, repo=args.repo, tag=args.tag) else 0
    except (RailDataError, OSError) as exc:
        # OSError because a full disk is the realistic one: it surfaces out of
        # the manifest write, after the per-region lines have already been
        # printed, and an operator reading a traceback there would not know
        # that the stores themselves are intact.
        _log(f"error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
