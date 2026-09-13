# data/

Nothing in here is committed except `samples/`.

## Files the app looks for

| Path | What | Where from |
|---|---|---|
| `chr.csv`, or any `*County Health Rankings*.xlsx` | County Health Rankings annual release | [countyhealthrankings.org/health-data](https://www.countyhealthrankings.org/health-data) — public download |
| `zip_county.csv` | HUD USPS ZIP-to-county crosswalk | [huduser.gov](https://www.huduser.gov/portal/datasets/usps_crosswalk.html) — free account and API token required |
| `chr_slim.csv` | Written automatically. A 5-column cache of the workbook so later runs skip the ~5s Excel parse. Delete it to force a re-read. | generated |

Override either location with `DAYLIGHT_CHR_PATH` / `DAYLIGHT_ZIP_COUNTY_PATH`.

`enrichment.py` reads `.csv`, `.xlsx`, and `.xls`, and resolves column names against candidate
lists — both the human-readable CHR headers and the coded `v###_rawvalue` style work without
renaming anything.

## Columns actually used

From the 2025 CHR workbook, which splits them across two sheets:

- **Additional Measure Data** → `% Frequent Mental Distress` (percent, 12.0–26.7)
- **Select Measure Data** → `Mental Health Provider Rate` (per 100,000, mean ≈ 212)

Joined on `FIPS`, a zero-padded 5-character string. Rows ending in `000` are state-level
aggregates and are dropped.

From the HUD crosswalk: `ZIP`, `COUNTY` (5-digit county FIPS), `RES_RATIO`. Where a zip
straddles counties, the highest residential ratio wins.

## samples/

Committed fixtures so `evals/run_evals.py` runs against known values with no real data present.

- `chr_sample.csv` — 6 counties, one per eval context type
- `zip_county_sample.csv` — 6 zips in HUD's column shape, including one split zip (13413 →
  Oneida at .82 / Madison at .18) to exercise the residential-ratio tie-break

| zip | county | context_type |
|---|---|---|
| 40906 | Knox, KY | `high_distress` (20.8% distress) |
| 35953 | St. Clair, AL | `provider_shortage` (29.3 providers/100k) |
| 94110 | San Francisco, CA | `default` |
| 13413 | Oneida, NY | `default` (split zip) |
| 02134 | Suffolk, MA | `default` |
| 99999 | — | `default` (not found) |
