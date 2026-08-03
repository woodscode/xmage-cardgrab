#!/usr/bin/env python3
"""
xmage_scryfall_images.py

Downloads MTG card images from Scryfall and writes them using XMage's exact
on-disk naming convention, so the XMage client picks them up as already-present.

WHY THIS EXISTS
---------------
XMage's ScryfallImageSource fetches https://api.scryfall.com/bulk-data/all-cards
to build its download manifest. Older release builds expect the deprecated
`download_uri` / single-JSON-array bulk format. Scryfall completed its move to
gzipped JSONL on 2026-07-20, so those builds log:

    ERROR Unknown bulk info format from scryfall api https://api.scryfall.com/bulk-data/all-cards

and abort every download thread immediately. Tracked as magefree/mage#15550.

NOTE: master has ALREADY been fixed (it reads `jsonl_download_uri`). If you can
run a dev/beta build, that is the better fix. This script is for staying on a
release build.

NAMING CONVENTION (replicated from Mage.Client CardImageUtils.java)
-------------------------------------------------------------------
  buildImagePathToCardOrToken():
      <imagesDir>/<SET>/<Name>[ <imageNumber>][.<collectorId>].full.jpg
  zip mode:
      <imagesDir>/<SET>.zip  -> internal path  <SET>/<Name>...full.jpg

  * SET is uppercased. "CON" is rewritten to "COX" (Windows reserved name).
  * prepareCardNameForFile(): "//" -> "-", then strip  \\ / : * ? " < > |
  * getCollectorIdAsFileName(): "*" and "\u2605" -> "star"
  * The ".<collectorId>" segment is present only when the card usesVariousArt.
  * imageNumber is 0 for normal cards (only tokens use it), so no numeric infix.
  * XMage requests image quality "large".

CAVEAT ON usesVariousArt
------------------------
usesVariousArt lives in XMage's own card database, not in Scryfall data. This
script infers it: if a card name appears more than once within the same set,
it is treated as various-art and gets the collector-id segment. That matches
the semantics and is right for the overwhelming majority of cards. Use
--dup-fallback to additionally write a plain <Name>.full.jpg copy for such
groups, which guarantees a hit regardless of what XMage's DB thinks.

TOKENS ARE SKIPPED. XMage keeps tokens under TOK/<SET>/ driven by its own token
repository with imageNumber suffixes that do not map cleanly onto Scryfall data.
Let the client download those itself.

USAGE
-----
  # test with a couple of sets first
  python3 xmage_scryfall_images.py --out ./images --sets DSK,BLB

  # full english run, writing straight into your XMage images folder
  python3 xmage_scryfall_images.py --out "/path/to/xmage/mage-client/plugins/images"

  # zip mode (only if "store images in zip files" is ENABLED in preferences)
  python3 xmage_scryfall_images.py --out ./images --zip

IMPORTANT XMAGE PREFERENCE ALIGNMENT
------------------------------------
Preferences -> Images:
  * "Store images in zip files" must MATCH whether you used --zip.
  * Turning off "check for new images at startup" avoids the broken bulk call.
"""

import argparse
import gzip
import io
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict

BULK_API = "https://api.scryfall.com/bulk-data"
USER_AGENT = "XMageScryfallImageHelper/1.0 (personal XMage image cache)"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json;q=0.9,*/*;q=0.8",
}

# XMage requests "large" (ScryfallImageSource.getImageQuality)
DEFAULT_QUALITY = "large"

# DownloadPicturesService.MIN_FILE_SIZE_OF_GOOD_IMAGE = 1024 * 6
# Anything smaller is treated by XMage as a broken file, so we treat it the
# same way on resume and re-fetch it instead of counting it as present.
MIN_GOOD_SIZE = 1024 * 6

# ---------------------------------------------------------------- name mangling


def prepare_card_name_for_file(card_name: str) -> str:
    """Port of CardImageUtils.prepareCardNameForFile (order matters)."""
    s = card_name.replace("//", "-")
    for ch in ("\\", "/", ":", "*", "?", '"', "<", ">", "|"):
        s = s.replace(ch, "")
    return s


def collector_id_as_file_name(collector_id: str) -> str:
    """Port of CardDownloadData.getCollectorIdAsFileName."""
    return collector_id.replace("*", "star").replace("\u2605", "star")


def fix_set_name_for_windows(set_code: str) -> str:
    """Port of CardImageUtils.fixSetNameForWindows."""
    return "COX" if set_code.upper() == "CON" else set_code


# ---------------------------------------------------------------- bulk fetching


def http_get_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_bulk_entry(bulk_type):
    data = http_get_json(BULK_API)
    for entry in data.get("data", []):
        if entry.get("type") == bulk_type:
            return entry
    avail = [e.get("type") for e in data.get("data", [])]
    raise SystemExit(f"bulk type '{bulk_type}' not found. available: {avail}")


def download_bulk_file(url, cache_path):
    """Download the .jsonl.gz once and cache it locally."""
    if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
        print(f"Using cached bulk file: {cache_path}", file=sys.stderr)
        return cache_path

    os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
    tmp = cache_path + ".part"
    print(f"Downloading bulk file -> {cache_path}", file=sys.stderr)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as out:
        total = 0
        while True:
            chunk = resp.read(4 * 1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            total += len(chunk)
            print(f"\r  {total / (1024*1024):.1f} MB", end="", file=sys.stderr)
    print("", file=sys.stderr)
    os.replace(tmp, cache_path)
    return cache_path


def iter_jsonl_gz(path):
    with gzip.open(path, "rb") as gz:
        for raw in gz:
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                continue


# ---------------------------------------------------------------- entry building


def build_entries(bulk_path, quality, lang, set_filter, include_digital):
    """
    Mirrors ScryfallImageSource.createBulkImagesIndex:
      - index the main card under card.name
      - index each face under face.name (dedupe repeated names; flip-card skip)
    Returns list of dicts: {set, name, collector, url}
    """
    entries = []
    seen_keys = set()
    scanned = 0
    dup_skipped = 0

    for card in iter_jsonl_gz(bulk_path):
        scanned += 1
        if scanned % 100000 == 0:
            print(f"  scanned {scanned} bulk records...", file=sys.stderr)

        if lang != "all" and card.get("lang") != lang:
            continue

        set_code = (card.get("set") or "").upper()
        if not set_code:
            continue
        if set_filter and set_code not in set_filter:
            continue
        if not include_digital and card.get("digital"):
            continue

        collector = str(card.get("collector_number", ""))
        if not collector:
            continue

        layout = card.get("layout", "")

        def add(name, url):
            nonlocal dup_skipped
            key = (name, set_code, collector)
            if key in seen_keys:
                dup_skipped += 1
                return False
            seen_keys.add(key)
            entries.append({"set": set_code, "name": name,
                            "collector": collector, "url": url})
            return True

        # main card image
        main_uris = card.get("image_uris") or {}
        main_url = main_uris.get(quality)
        main_added = False
        if main_url:
            main_added = add(card.get("name", ""), main_url)

        # per-face images
        faces = card.get("card_faces") or []
        used_face_names = set()
        for face in faces:
            fname = face.get("name", "")
            if not fname or fname in used_face_names:
                continue
            used_face_names.add(fname)

            face_url = (face.get("image_uris") or {}).get(quality)
            if not face_url:
                continue

            # flip-card workaround from XMage: first face duplicates the main entry
            if layout == "flip" and card.get("name") == fname and main_added:
                continue

            add(fname, face_url)

    print(f"  scanned {scanned} records, {len(entries)} image entries, "
          f"{dup_skipped} duplicate keys skipped", file=sys.stderr)
    return entries


def assign_filenames(entries, dup_fallback):
    """
    Decide final filename per entry using XMage's usesVariousArt rule.
    Heuristic: duplicate (set, prepared name) within a set => various art.
    """
    groups = defaultdict(list)
    for e in entries:
        e["fname_base"] = prepare_card_name_for_file(e["name"])
        groups[(e["set"], e["fname_base"])].append(e)

    jobs = []
    for (set_code, base), group in groups.items():
        various = len(group) > 1
        for i, e in enumerate(group):
            set_dir = fix_set_name_for_windows(set_code)
            if various:
                cid = collector_id_as_file_name(e["collector"])
                fname = f"{base}.{cid}.full.jpg"
            else:
                fname = f"{base}.full.jpg"
            jobs.append({"set_dir": set_dir, "fname": fname, "url": e["url"]})

            # optional safety net: also write the plain name for the first
            # member of a various-art group, in case XMage's DB disagrees
            if various and dup_fallback and i == 0:
                jobs.append({"set_dir": set_dir,
                             "fname": f"{base}.full.jpg",
                             "url": e["url"]})
    return jobs


# ---------------------------------------------------------------- downloading


class RateLimiter:
    """Simple global token-bucket-ish limiter. Be polite to Scryfall."""

    def __init__(self, per_second):
        self.interval = 1.0 / per_second if per_second > 0 else 0.0
        self.lock = threading.Lock()
        self.next_time = time.monotonic()

    def wait(self):
        if self.interval <= 0:
            return
        with self.lock:
            now = time.monotonic()
            if self.next_time < now:
                self.next_time = now
            delay = self.next_time - now
            self.next_time += self.interval
        if delay > 0:
            time.sleep(delay)


def worker(job_q, out_dir, stats, lock, limiter, stop_event):
    while not stop_event.is_set():
        try:
            job = job_q.get(timeout=1)
        except queue.Empty:
            return
        dest = os.path.join(out_dir, job["set_dir"], job["fname"])
        try:
            # Resume check: present AND big enough to satisfy XMage.
            # A file under 6KB would be rejected by the client as broken, so
            # we re-fetch it rather than skipping it.
            if os.path.exists(dest):
                size = os.path.getsize(dest)
                if size >= MIN_GOOD_SIZE:
                    with lock:
                        stats["skipped"] += 1
                    continue
                with lock:
                    stats["repaired"] += 1  # too small, will re-download below

            os.makedirs(os.path.dirname(dest), exist_ok=True)

            last_err = None
            for attempt in range(3):
                try:
                    limiter.wait()
                    req = urllib.request.Request(job["url"], headers=HEADERS)
                    with urllib.request.urlopen(req, timeout=45) as resp:
                        data = resp.read()
                    if not data or len(data) < MIN_GOOD_SIZE:
                        raise IOError(f"response too small ({len(data)} bytes)")
                    tmp = dest + ".tmp"
                    with open(tmp, "wb") as f:
                        f.write(data)
                    os.replace(tmp, dest)  # atomic, so no partial files on Ctrl-C
                    with lock:
                        stats["downloaded"] += 1
                    last_err = None
                    break
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    code = getattr(e, "code", None)
                    if code in (404, 403):
                        break  # genuinely absent, retrying will not help
                    if attempt < 2:
                        time.sleep(1.5 * (2 ** attempt))  # 1.5s, 3s
            if last_err is not None:
                raise last_err
        except Exception as e:  # noqa: BLE001 - keep the run alive
            with lock:
                stats["failed"] += 1
                if len(stats["errors"]) < 50:
                    stats["errors"].append(f"{job['set_dir']}/{job['fname']}: {e}")
        finally:
            job_q.task_done()
            with lock:
                done = stats["downloaded"] + stats["skipped"] + stats["failed"]
            if done % 500 == 0:
                print(f"  {done} processed "
                      f"(new={stats['downloaded']} have={stats['skipped']} "
                      f"fail={stats['failed']})", file=sys.stderr)


def zip_sets(out_dir):
    """Repack each <SET>/ folder into <SET>.zip containing <SET>/ internally."""
    for name in sorted(os.listdir(out_dir)):
        set_path = os.path.join(out_dir, name)
        if not os.path.isdir(set_path) or name.endswith(".zip"):
            continue
        zip_path = os.path.join(out_dir, f"{name}.zip")
        print(f"  zipping {name} -> {os.path.basename(zip_path)}", file=sys.stderr)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            for root, _dirs, files in os.walk(set_path):
                for fn in files:
                    full = os.path.join(root, fn)
                    rel = os.path.relpath(full, out_dir)
                    zf.write(full, arcname=rel)
        shutil.rmtree(set_path)


# ---------------------------------------------------------------- main


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", required=True,
                   help="XMage images dir (e.g. .../mage-client/plugins/images)")
    p.add_argument("--type", default="default_cards",
                   choices=["default_cards", "all_cards", "unique_artwork"],
                   help="Scryfall bulk type. default_cards = one per printing")
    p.add_argument("--lang", default="en", help="language code, or 'all'")
    p.add_argument("--quality", default=DEFAULT_QUALITY,
                   choices=["small", "normal", "large"],
                   help="XMage uses 'large'")
    p.add_argument("--sets", default="",
                   help="comma-separated set codes to limit to (e.g. DSK,BLB)")
    p.add_argument("--threads", type=int, default=6)
    p.add_argument("--rate", type=float, default=10.0,
                   help="max image requests per second overall (be polite)")
    p.add_argument("--zip", action="store_true",
                   help="pack into <SET>.zip (match the zip preference!)")
    p.add_argument("--dup-fallback", action="store_true",
                   help="also write plain <Name>.full.jpg for various-art groups")
    p.add_argument("--include-digital", action="store_true",
                   help="include digital-only printings (MTGO/Arena)")
    p.add_argument("--cache", default="./scryfall_bulk_cache.jsonl.gz")
    p.add_argument("--dry-run", action="store_true",
                   help="print what would be written, download nothing")
    args = p.parse_args()

    set_filter = {s.strip().upper() for s in args.sets.split(",") if s.strip()}

    entry = get_bulk_entry(args.type)
    url = entry.get("jsonl_download_uri") or entry.get("download_uri")
    if not url:
        raise SystemExit("no jsonl_download_uri in bulk entry; API changed again")
    print(f"Bulk: {entry.get('name')}  updated {entry.get('updated_at')}",
          file=sys.stderr)

    bulk_path = download_bulk_file(url, args.cache)

    print("Indexing cards...", file=sys.stderr)
    entries = build_entries(bulk_path, args.quality, args.lang,
                            set_filter, args.include_digital)
    if not entries:
        raise SystemExit("No matching cards. Check --sets / --lang.")

    jobs = assign_filenames(entries, args.dup_fallback)
    print(f"{len(jobs)} files to place across "
          f"{len({j['set_dir'] for j in jobs})} sets", file=sys.stderr)

    if args.dry_run:
        for j in jobs[:40]:
            print(f"  {j['set_dir']}/{j['fname']}")
        if len(jobs) > 40:
            print(f"  ... and {len(jobs) - 40} more")
        return

    os.makedirs(args.out, exist_ok=True)
    job_q = queue.Queue(maxsize=10000)
    stats = {"downloaded": 0, "skipped": 0, "failed": 0, "repaired": 0, "errors": []}
    lock = threading.Lock()
    limiter = RateLimiter(args.rate)
    stop_event = threading.Event()

    workers = [threading.Thread(target=worker,
                                args=(job_q, args.out, stats, lock, limiter, stop_event),
                                daemon=True)
               for _ in range(args.threads)]
    for w in workers:
        w.start()

    try:
        for j in jobs:
            job_q.put(j)
        job_q.join()
    except KeyboardInterrupt:
        print("\nInterrupted; finishing in-flight downloads...", file=sys.stderr)
    finally:
        stop_event.set()

    if args.zip:
        print("Packing zips...", file=sys.stderr)
        zip_sets(args.out)

    print("\nDone.")
    print(f"  downloaded : {stats['downloaded']}")
    print(f"  already had: {stats['skipped']}")
    print(f"  re-fetched : {stats['repaired']} (were under 6KB, XMage would reject)")
    print(f"  failed     : {stats['failed']}")
    if stats["errors"]:
        print("\n  first errors:")
        for e in stats["errors"][:10]:
            print(f"    {e}")


if __name__ == "__main__":
    main()
