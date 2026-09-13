"""Community-level mental health enrichment.

Turns a hero's zip code into county context. Two hops:

    zip -> county FIPS   (HUD USPS zip-to-county crosswalk)
    FIPS -> metrics      (County Health Rankings annual release)

The output shapes *tone*, never output. Nothing in this module ever reaches the hero as a
number — `context_type` selects one of three sentences injected into the system prompt, and
that is the entire contract with the conversation layer. See SPEC.md, "Data informs voice,
not output".

Failure is always silent and always "default". A hero mid-conversation must never see a
stack trace because a CSV moved, so every public function in here returns the default
enrichment dict rather than raising.

Data notes (2025 release, verified against the real workbook)
------------------------------------------------------------
* The two metrics the spec names live on *different sheets* of the CHR workbook:
  `% Frequent Mental Distress` on "Additional Measure Data", the provider rate on
  "Select Measure Data". Both sheets use a two-row header — the real column names are on
  row 2 (`header=1`).
* Both are already in the units the spec's thresholds assume: distress is a percent
  (12.0-26.7 across counties), the provider rate is per 100,000 (mean ~212). No scaling.
* FIPS is a zero-padded 5-character string. Rows whose FIPS ends in "000" are state-level
  aggregates and are dropped — a hero's zip must resolve to a county, not a state.
"""

from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

# ---------------------------------------------------------------------------
# Classification thresholds (SPEC.md -> Context classification)
# ---------------------------------------------------------------------------

HIGH_DISTRESS_THRESHOLD = 20.0   # % of adults with 14+ poor mental health days/month
PROVIDER_SHORTAGE_THRESHOLD = 30.0  # mental health providers per 100k population

#: Returned whenever we cannot enrich — unknown zip, missing file, unreadable file.
DEFAULT_CONTEXT: dict = {
    "county": None,
    "state": None,
    "pct_frequent_distress": None,
    "mh_providers_per_100k": None,
    "context_type": "default",
}

# ---------------------------------------------------------------------------
# Column resolution
#
# The CHR release renames and re-groups columns between annual editions, and the HUD
# crosswalk ships with different casing depending on whether you pull the CSV or the XLSX.
# Rather than hardcode one spelling and break on next year's file, each logical field has a
# candidate list; the first candidate present in the sheet wins.
# ---------------------------------------------------------------------------

CHR_SHEETS = ("Select Measure Data", "Additional Measure Data")

_FIPS_CANDIDATES = ("FIPS", "fipscode", "5-digit FIPS Code", "fips")
_COUNTY_CANDIDATES = ("County", "county", "Name")
_STATE_CANDIDATES = ("State", "state", "State Abbreviation")

_DISTRESS_CANDIDATES = (
    "% Frequent Mental Distress",
    "Frequent mental distress raw value",
    "v145_rawvalue",
    "% Frequent mental distress",
)
_PROVIDER_CANDIDATES = (
    "Mental Health Provider Rate",
    "Mental Health Providers per 100K",
    "Mental health providers raw value",
    "v062_rawvalue",
    "MHP Rate",
)

_ZIP_CANDIDATES = ("ZIP", "zip", "Zip", "ZIP_CODE", "zipcode", "zip_code")
_XWALK_FIPS_CANDIDATES = ("COUNTY", "county", "FIPS", "fips", "county_fips", "GEOID")
_RATIO_CANDIDATES = ("RES_RATIO", "res_ratio", "ResRatio", "TOT_RATIO", "tot_ratio")


def _resolve(columns, candidates: tuple[str, ...]) -> str | None:
    """First candidate present in `columns`, matched case-insensitively."""
    lookup = {str(c).strip().lower(): c for c in columns}
    for candidate in candidates:
        hit = lookup.get(candidate.strip().lower())
        if hit is not None:
            return hit
    return None


def _normalize_fips(series: pd.Series) -> pd.Series:
    """Coerce a FIPS column to zero-padded 5-character strings.

    Excel helpfully turns "01001" into the integer 1001, so string-cast alone is not enough.
    """
    cleaned = series.astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    numeric = cleaned.str.fullmatch(r"\d+")
    cleaned = cleaned.where(~numeric.fillna(False), cleaned.str.zfill(5))
    return cleaned


def _normalize_zip(value) -> str | None:
    """Coerce anything zip-shaped to a 5-digit string, or None.

    Accepts "02134", 2134, "02134-1234", " 02134 ". ZIP+4 is truncated to the 5-digit
    prefix, which is what the crosswalk is keyed on.
    """
    if value is None:
        return None
    text = re.sub(r"\D", "", str(value).strip())
    if not text:
        return None
    text = text[:9]
    if len(text) > 5:
        text = text[:5]
    return text.zfill(5) if len(text) <= 5 else None


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------


def _first_existing(*paths: Path | str | None) -> Path | None:
    for path in paths:
        if not path:
            continue
        candidate = Path(path).expanduser()
        if candidate.is_file():
            return candidate
    return None


def chr_path() -> Path | None:
    """Locate the County Health Rankings release.

    Resolution order: `DAYLIGHT_CHR_PATH`, then the slim CSV cache this module writes, then
    the spec's `data/chr.csv`, then any CHR-looking workbook dropped into `data/`.

    The slim cache is skipped when a source file in `data/` is newer than it. Without that
    check, dropping next year's release into `data/` would silently keep serving last year's
    numbers — the cache would win on every run and nothing would look wrong.
    """
    explicit = os.getenv("DAYLIGHT_CHR_PATH")
    if explicit:
        found = _first_existing(explicit)
        if found:
            return found

    sources = [
        path
        for path in [DATA_DIR / "chr.csv", *sorted(DATA_DIR.glob("*County Health Rankings*.xls*"))]
        if path.is_file()
    ]
    slim = DATA_DIR / "chr_slim.csv"
    if slim.is_file():
        newer = [p for p in sources if p.stat().st_mtime > slim.stat().st_mtime]
        if not newer:
            return slim
        log.info("%s is newer than the slim cache; re-reading it", newer[-1].name)

    return sources[-1] if sources else None


def zip_county_path() -> Path | None:
    """Locate the zip-to-county crosswalk."""
    return _first_existing(
        os.getenv("DAYLIGHT_ZIP_COUNTY_PATH"),
        DATA_DIR / "zip_county.csv",
        *sorted(DATA_DIR.glob("*ZIP_COUNTY*.xls*")),
        *sorted(DATA_DIR.glob("*zip_county*.csv")),
    )


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def _read_tabular(path: Path, **kwargs) -> pd.DataFrame:
    """Read a CSV or Excel file, whichever this is."""
    if path.suffix.lower() in {".xlsx", ".xls", ".xlsm"}:
        return pd.read_excel(path, **kwargs)
    kwargs.pop("sheet_name", None)
    kwargs.pop("header", None)
    return pd.read_csv(path, **kwargs)


def _load_chr_workbook(path: Path) -> pd.DataFrame:
    """Pull the two metric columns off the two sheets of a CHR workbook and merge on FIPS."""
    frames: list[pd.DataFrame] = []
    for sheet in CHR_SHEETS:
        try:
            header = pd.read_excel(path, sheet_name=sheet, header=1, nrows=0)
        except ValueError:
            continue  # sheet absent in this release
        fips_col = _resolve(header.columns, _FIPS_CANDIDATES)
        if not fips_col:
            continue

        wanted = {fips_col: "fips"}
        for target, candidates in (
            ("county", _COUNTY_CANDIDATES),
            ("state", _STATE_CANDIDATES),
            ("pct_frequent_distress", _DISTRESS_CANDIDATES),
            ("mh_providers_per_100k", _PROVIDER_CANDIDATES),
        ):
            col = _resolve(header.columns, candidates)
            if col:
                wanted[col] = target

        # Only read the columns we need — the 2025 workbook is 396 columns wide.
        frame = pd.read_excel(
            path, sheet_name=sheet, header=1, usecols=list(wanted), dtype={fips_col: str}
        ).rename(columns=wanted)
        frames.append(frame)
        log.debug("CHR sheet %r contributed %s", sheet, sorted(wanted.values()))

    if not frames:
        raise ValueError(f"no recognizable CHR sheets in {path.name}")

    merged = frames[0]
    for frame in frames[1:]:
        new_cols = [c for c in frame.columns if c not in merged.columns or c == "fips"]
        merged = merged.merge(frame[new_cols], on="fips", how="outer")
    return merged


def _load_chr_flat(path: Path) -> pd.DataFrame:
    """Read a single-table CHR file (the slim cache, or a pre-flattened CSV export)."""
    frame = _read_tabular(path, dtype=str)
    wanted = {}
    for target, candidates in (
        ("fips", _FIPS_CANDIDATES + ("fips",)),
        ("county", _COUNTY_CANDIDATES),
        ("state", _STATE_CANDIDATES),
        ("pct_frequent_distress", _DISTRESS_CANDIDATES + ("pct_frequent_distress",)),
        ("mh_providers_per_100k", _PROVIDER_CANDIDATES + ("mh_providers_per_100k",)),
    ):
        col = _resolve(frame.columns, candidates)
        if col:
            wanted[col] = target
    if "fips" not in wanted.values():
        raise ValueError(f"no FIPS column in {path.name}")
    return frame[list(wanted)].rename(columns=wanted)


@lru_cache(maxsize=1)
def load_chr(path: str | None = None) -> pd.DataFrame:
    """County Health Rankings metrics, indexed by 5-digit county FIPS.

    Cached for the process. Reading the raw 2025 workbook takes ~5s, so on the first
    successful workbook load a slim CSV is written to `data/chr_slim.csv` (gitignored) and
    picked up by `chr_path()` on subsequent runs, which drops the load to milliseconds.

    Returns an empty DataFrame if the release is missing or unreadable — callers degrade to
    DEFAULT_CONTEXT rather than failing.
    """
    resolved = Path(path) if path else chr_path()
    if resolved is None:
        log.warning("County Health Rankings data not found; enrichment disabled")
        return pd.DataFrame()

    try:
        is_workbook = resolved.suffix.lower() in {".xlsx", ".xls", ".xlsm"}
        frame = _load_chr_workbook(resolved) if is_workbook else _load_chr_flat(resolved)
    except Exception:
        log.exception("could not read CHR data at %s; enrichment disabled", resolved)
        return pd.DataFrame()

    frame["fips"] = _normalize_fips(frame["fips"])
    for column in ("pct_frequent_distress", "mh_providers_per_100k"):
        if column not in frame.columns:
            frame[column] = pd.NA
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    for column in ("county", "state"):
        if column not in frame.columns:
            frame[column] = pd.NA

    # Drop state-level aggregate rows and anything without a usable key.
    frame = frame[frame["fips"].str.fullmatch(r"\d{5}").fillna(False)]
    frame = frame[~frame["fips"].str.endswith("000")]
    frame = frame.dropna(subset=["fips"]).drop_duplicates(subset=["fips"], keep="first")
    frame = frame.set_index("fips")

    if is_workbook and not frame.empty:
        _write_slim_cache(frame)

    log.info("loaded %d counties of CHR data from %s", len(frame), resolved.name)
    return frame


def _write_slim_cache(frame: pd.DataFrame) -> None:
    """Persist the five columns we actually use, so we never re-parse 15MB of Excel."""
    target = DATA_DIR / "chr_slim.csv"
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        frame.reset_index().to_csv(target, index=False)
        log.info("wrote slim CHR cache to %s", target)
    except OSError:
        log.debug("could not write slim CHR cache (harmless)", exc_info=True)


@lru_cache(maxsize=1)
def load_zip_county(path: str | None = None) -> pd.DataFrame:
    """Zip-to-county crosswalk: one winning county FIPS per zip.

    A zip can straddle county lines, so the spec says to take the county with the highest
    residential ratio. HUD's file ships that as `RES_RATIO`; if the column is absent we keep
    the first row per zip and log it, because an arbitrary pick is better than no enrichment
    and this only shifts tone, never output.

    Returns an empty DataFrame when the crosswalk is missing — which means every session
    falls back to DEFAULT_CONTEXT. The HUD file is token-gated at huduser.gov and is not
    committed to this repo; see data/README.md.
    """
    resolved = Path(path) if path else zip_county_path()
    if resolved is None:
        log.warning("zip-to-county crosswalk not found; enrichment disabled")
        return pd.DataFrame()

    try:
        frame = _read_tabular(resolved, dtype=str)
    except Exception:
        log.exception("could not read crosswalk at %s; enrichment disabled", resolved)
        return pd.DataFrame()

    zip_col = _resolve(frame.columns, _ZIP_CANDIDATES)
    fips_col = _resolve(frame.columns, _XWALK_FIPS_CANDIDATES)
    if not zip_col or not fips_col:
        log.error("crosswalk %s lacks zip/county columns: %s",
                  resolved.name, list(frame.columns)[:12])
        return pd.DataFrame()

    ratio_col = _resolve(frame.columns, _RATIO_CANDIDATES)
    keep = {zip_col: "zip", fips_col: "fips"}
    if ratio_col:
        keep[ratio_col] = "res_ratio"
    frame = frame[list(keep)].rename(columns=keep)

    frame["zip"] = frame["zip"].map(_normalize_zip)
    frame["fips"] = _normalize_fips(frame["fips"])
    frame = frame.dropna(subset=["zip", "fips"])

    if "res_ratio" in frame.columns:
        frame["res_ratio"] = pd.to_numeric(frame["res_ratio"], errors="coerce").fillna(0.0)
        frame = frame.sort_values("res_ratio", ascending=False)
    else:
        log.info("crosswalk %s has no residential-ratio column; taking first county per zip",
                 resolved.name)

    frame = frame.drop_duplicates(subset=["zip"], keep="first").set_index("zip")
    log.info("loaded %d zips from %s", len(frame), resolved.name)
    return frame


# ---------------------------------------------------------------------------
# Classification + lookup
# ---------------------------------------------------------------------------


def classify(
    pct_frequent_distress: float | None, mh_providers_per_100k: float | None
) -> str:
    """Map county metrics to one of the three prompt-injection contexts.

    `high_distress` is checked first, so a county that is both high-distress and
    provider-short is treated as high-distress. That ordering follows the spec's listing
    order and is a deliberate choice: the "many people near you are carrying this without
    support" framing speaks to the hero's isolation, which is the product's core message,
    while the provider-shortage framing speaks to logistics.
    """
    if pct_frequent_distress is not None and pct_frequent_distress > HIGH_DISTRESS_THRESHOLD:
        return "high_distress"
    if (
        mh_providers_per_100k is not None
        and mh_providers_per_100k < PROVIDER_SHORTAGE_THRESHOLD
    ):
        return "provider_shortage"
    return "default"


def _clean_metric(value) -> float | None:
    if value is None or pd.isna(value):
        return None
    return round(float(value), 1)


def lookup(zip_code: str | None) -> dict:
    """Enrich a zip code with county mental health context.

    Never raises. An unknown zip, a missing data file, or a malformed release all produce
    DEFAULT_CONTEXT, and the conversation proceeds with no community injection at all.

    Returns the dict specified in SPEC.md -> Community Enrichment:
        {county, state, pct_frequent_distress, mh_providers_per_100k, context_type}
    """
    normalized = _normalize_zip(zip_code)
    if not normalized:
        return dict(DEFAULT_CONTEXT)

    try:
        crosswalk = load_zip_county()
        if crosswalk.empty or normalized not in crosswalk.index:
            log.debug("zip %s not in crosswalk", normalized)
            return dict(DEFAULT_CONTEXT)
        fips = crosswalk.at[normalized, "fips"]

        chr_data = load_chr()
        if chr_data.empty or fips not in chr_data.index:
            log.debug("FIPS %s not in CHR data", fips)
            return dict(DEFAULT_CONTEXT)
        row = chr_data.loc[fips]

        distress = _clean_metric(row.get("pct_frequent_distress"))
        providers = _clean_metric(row.get("mh_providers_per_100k"))
        county = row.get("county")
        state = row.get("state")

        return {
            "county": None if pd.isna(county) else str(county),
            "state": None if pd.isna(state) else str(state),
            "pct_frequent_distress": distress,
            "mh_providers_per_100k": providers,
            "context_type": classify(distress, providers),
        }
    except Exception:
        # Belt and braces. Nothing in an enrichment lookup is worth interrupting a hero for.
        log.exception("enrichment failed for zip %s", normalized)
        return dict(DEFAULT_CONTEXT)


def warm_cache() -> None:
    """Pre-load both data files.

    Called from the welcome screen so the ~5s first read of the CHR workbook happens while
    the hero is reading the disclaimer, not mid-sentence.
    """
    try:
        load_chr()
        load_zip_county()
    except Exception:
        log.debug("cache warm failed (harmless)", exc_info=True)
