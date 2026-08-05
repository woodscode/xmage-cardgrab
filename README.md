# xmage-cardgrab

Downloads MTG card images from Scryfall and writes them using XMage's exact
on-disk naming convention, so the client sees them as already downloaded.

## Why

XMage's `ScryfallImageSource` builds its download manifest from
`https://api.scryfall.com/bulk-data/all-cards`. Release builds up to and
including `1.4.60V3` (2026-07-11) expect the deprecated `download_uri` /
single-JSON-array bulk format.

Scryfall completed its migration to gzipped JSONL on **2026-07-20**, nine days
after that release. Affected clients log:

```
ERROR Unknown bulk info format from scryfall api https://api.scryfall.com/bulk-data/all-cards
      =>[Thread-34] ScryfallImageSource.prepareBulkData
```

and every download thread aborts immediately. Upstream issue:
[magefree/mage#15550](https://github.com/magefree/mage/issues/15550).

**The fix is already merged to master** (`ScryfallImageSource` reads
`jsonl_download_uri`), it just has not been cut into a release yet. Once a
build ships with it, this tool is obsolete. Images written by this tool remain
valid, because they use XMage's native naming, so the client will simply see
them as present.

## Requirements

Python 3.8+. Standard library only, no dependencies.

## Usage

Dry run first, one set, to confirm naming:

```bash
python3 xmage_scryfall_images.py --out ./images --sets ISD --dry-run
```

Real run into your XMage images folder:

```bash
python3 xmage_scryfall_images.py \
  --out "/path/to/xmage/mage-client/plugins/images" \
  --quality normal --threads 16 --rate 0
```

For Windows/Powershell
```bash
python3 xmage_scryfall_images.py --out "C:\Users\youruser\downloadlocation\mage-full_1.4.60-dev_2026-07-11_16-06\xmage\mage-client\plugins\images" --quality normal --threads 16 --rate 0
```
Re-run the identical command afterwards to mop up any failures. It is
resumable and only fetches what is missing or broken.

### Options

| Flag | Default | Notes |
| --- | --- | --- |
| `--out` | required | XMage images dir, e.g. `.../mage-client/plugins/images` |
| `--type` | `default_cards` | `default_cards`, `all_cards`, `unique_artwork` |
| `--lang` | `en` | language code, or `all` |
| `--quality` | `large` | `small` / `normal` / `large`. Does not affect filenames |
| `--sets` | all | comma-separated set codes, e.g. `DSK,BLB` |
| `--threads` | `6` | 16 is reasonable; past ~24 you are bandwidth bound |
| `--rate` | `10.0` | requests/sec. `0` disables (see rate limits below) |
| `--zip` | off | pack into `<SET>.zip`. Must match the client preference |
| `--dup-fallback` | off | also write plain `<Name>.full.jpg` for various-art groups |
| `--include-digital` | off | include MTGO/Arena-only printings |
| `--cache` | `./scryfall_bulk_cache.jsonl.gz` | bulk file cache |
| `--dry-run` | off | print planned paths, download nothing |

Rough sizes for a full English run: `normal` ~10 GB, `large` ~25 GB.

## Rate limits

Scryfall asks for 50-100 ms between requests to `api.scryfall.com` (~10/sec),
but explicitly states that the file origins at `*.scryfall.io` have no such
limit. This tool makes exactly **one** call to `api.scryfall.com` (the
`/bulk-data` lookup); every image comes from `cards.scryfall.io`. So
`--rate 0 --threads 16` is fine and your bandwidth is the real constraint.

Please still be reasonable. Scryfall provides this free.

## Naming convention

Ported from `Mage.Client/.../utils/CardImageUtils.java`:

```
<imagesDir>/<SET>/<Name>.full.jpg
<imagesDir>/<SET>/<Name>.<collectorId>.full.jpg   # various-art cards
<imagesDir>/<SET>.zip -> <SET>/<Name>.full.jpg    # zip mode
```

- `SET` uppercased; `CON` rewritten to `COX` (Windows reserved name)
- `prepareCardNameForFile()`: `//` becomes `-`, then strip `\ / : * ? " < > |`
- `getCollectorIdAsFileName()`: `*` and `U+2605` become `star`
- Double-faced cards are written as separate files under each face name,
  mirroring `ScryfallImageSource.createBulkImagesIndex`

Detection on the client side is `buildImagePathToCardOrToken()` plus
`file.exists()` plus a 6 KB minimum size
(`DownloadPicturesService.MIN_FILE_SIZE_OF_GOOD_IMAGE`). This tool uses the
same 6 KB floor when deciding whether an existing file counts as present, so
re-runs repair undersized junk instead of skipping it forever.

## Caveats

**`usesVariousArt` is inferred.** It lives in XMage's own card database, not in
Scryfall data. This tool treats a card as various-art when the same name
appears more than once within a set, which is the correct semantics and right
for basics, alternate arts, Relentless Rats and so on. If a card still shows
blank, re-run with `--dup-fallback`, which additionally writes the plain
`<Name>.full.jpg` variant so one of the two always matches.

**Tokens are skipped.** XMage keeps tokens under `TOK/<SET>/` driven by its own
token repository with `imageNumber` suffixes that do not map cleanly onto
Scryfall data. Let the client handle those.

**Mana symbols are not handled and do not need to be.** `ScryfallSymbolsSource`
does not touch the bulk API. It scrapes the Scryfall docs page for a stylesheet
link and extracts base64 SVGs from the CSS, so the in-client symbol download
still works. Use main menu -> download symbols.

## Client preferences

Preferences -> Images:

- **"Store images in zip files"** must match whether you passed `--zip`.
  A mismatch means the client ignores everything you wrote.
- **"Check for new images at startup"** can be turned off to stop the client
  hitting the broken bulk endpoint on every launch.
- Consider unchecking **"use default location"** and pointing at a stable path
  outside the install directory, so images survive client updates.

## License

MIT. See [LICENSE](LICENSE).
