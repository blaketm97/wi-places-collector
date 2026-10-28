"""
Wisconsin CDC PLACES data collector.

Retrieves Wisconsin county records from the CDC PLACES Socrata API,
cleans and validates the data, summarizes age-adjusted diabetes prevalence,
and generates CSV, chart, quality-report, and root README outputs.
"""

import re
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import requests


# --------------------------------------------------
# Configuration
# --------------------------------------------------

DATASET_ID = "i46a-9kgh"
DATASET_NAME = "PLACES: County Data (GIS Friendly Format), 2025 release"
DATASET_PAGE = f"https://data.cdc.gov/d/{DATASET_ID}"
BASE_URL = f"https://data.cdc.gov/resource/{DATASET_ID}.json"
METADATA_URL = f"https://data.cdc.gov/api/views/{DATASET_ID}/columns.json"

STATE = "WI"
EXPECTED_COUNTIES = 72
PAGE_SIZE = 500

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

# README.md is written to the project root so GitHub displays it automatically.
README_PATH = Path("README.md")


# --------------------------------------------------
# Helpers
# --------------------------------------------------

def standardize_column(name):
    """Convert source field names to lowercase snake_case."""
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    name = re.sub(r"[^a-zA-Z0-9]+", "_", name)
    return name.strip("_").lower()


def fetch_json(url, params=None):
    """Fetch JSON from an endpoint, raising an exception on HTTP errors."""
    response = requests.get(url, params=params, timeout=60)
    response.raise_for_status()
    return response.json()


def get_metadata():
    """Retrieve Socrata column metadata for the generated data dictionary."""
    try:
        return fetch_json(METADATA_URL)
    except (requests.RequestException, ValueError) as error:
        print(f"Warning: Could not retrieve dataset metadata: {error}")
        return []


# --------------------------------------------------
# Download
# --------------------------------------------------

def download_data():
    """Retrieve every Wisconsin record using Socrata limit/offset pagination."""
    records = []
    offset = 0

    while True:
        params = {
            "$where": f"stateabbr = '{STATE}'",
            "$order": "countyfips",
            "$limit": PAGE_SIZE,
            "$offset": offset,
        }

        batch = fetch_json(BASE_URL, params=params)

        if not batch:
            break

        records.extend(batch)
        offset += len(batch)
        print(f"Downloaded {len(records)} records...")

        if len(batch) < PAGE_SIZE:
            break

    if not records:
        raise RuntimeError(
            "The API returned no records. Check the dataset page, API endpoint, "
            "and state filter."
        )

    raw = pd.DataFrame(records)
    raw.to_csv(OUTPUT_DIR / "wi_places_raw.csv", index=False)
    print(f"\nTotal records downloaded: {len(raw)}")
    return raw


# --------------------------------------------------
# Clean
# --------------------------------------------------

def clean_data(raw, metadata):
    """Standardize field names and convert metadata-defined numeric fields."""
    df = raw.copy()
    df.columns = [standardize_column(column) for column in df.columns]
    df = df.replace(r"^\s*$", pd.NA, regex=True)

    numeric_fields = set()
    for item in metadata:
        field = item.get("fieldName", "")
        data_type = str(item.get("dataType", "")).lower()
        if field and data_type in {"number", "money", "percent"}:
            numeric_fields.add(standardize_column(field))

    # Explicitly include key numeric fields in case metadata is unavailable.
    numeric_fields.update(
        column
        for column in df.columns
        if column in {"totalpopulation", "totalpop18plus"}
        or column.endswith("_crudeprev")
        or column.endswith("_adjprev")
    )

    for column in numeric_fields.intersection(df.columns):
        df[column] = pd.to_numeric(df[column], errors="coerce")

    # Confidence interval columns such as "(9.1, 10.2)" intentionally remain text.
    return df


# --------------------------------------------------
# Validate
# --------------------------------------------------

def validate_data(df):
    """Run basic quality checks and write a concise quality report."""
    df = df.copy()
    flags = pd.Series("", index=df.index, dtype="object")

    def add_flag(mask, label):
        mask = mask.fillna(False)
        flags.loc[mask] = flags.loc[mask].apply(
            lambda existing: "; ".join(
                part for part in (existing, label) if part
            )
        )

    # Domain-specific record count check: Wisconsin has 72 counties.
    record_count_ok = len(df) == EXPECTED_COUNTIES
    if not record_count_ok:
        print(
            f"WARNING: Expected {EXPECTED_COUNTIES} Wisconsin county records, "
            f"but downloaded {len(df)}."
        )

    if "countyfips" in df.columns:
        missing_id = df["countyfips"].isna()
        add_flag(missing_id, "Missing county FIPS")

        duplicate_id = (
            df["countyfips"].notna()
            & df["countyfips"].duplicated(keep=False)
        )
        add_flag(duplicate_id, "Duplicate county FIPS")

        duplicate_count = int(
            (
                df["countyfips"].notna()
                & df["countyfips"].duplicated()
            ).sum()
        )
    else:
        missing_id = pd.Series(True, index=df.index)
        duplicate_id = pd.Series(False, index=df.index)
        duplicate_count = 0
        add_flag(missing_id, "County FIPS column missing")

    # Check all crude and age-adjusted prevalence fields.
    prevalence_columns = [
        column
        for column in df.columns
        if column.endswith("_crudeprev") or column.endswith("_adjprev")
    ]

    invalid_prevalence_cells = 0
    invalid_prevalence_rows = pd.Series(False, index=df.index)

    for column in prevalence_columns:
        values = df[column]
        invalid = values.notna() & ((values < 0) | (values > 100))
        invalid_prevalence_cells += int(invalid.sum())
        invalid_prevalence_rows |= invalid
        add_flag(invalid, f"Out-of-range estimate: {column}")

    missing_cells = int(df.isna().sum().sum())
    missing_prevalence_cells = (
        int(df[prevalence_columns].isna().sum().sum())
        if prevalence_columns else 0
    )

    df["quality_flags"] = flags
    df["has_quality_issue"] = df["quality_flags"].ne("")

    report = [
        "CDC PLACES DATA QUALITY REPORT",
        "=" * 40,
        f"Dataset: {DATASET_NAME}",
        f"Source: {DATASET_PAGE}",
        f"State filter: {STATE}",
        f"Records retrieved: {len(df)}",
        (
            f"Expected Wisconsin county records: {EXPECTED_COUNTIES} "
            f"({'PASS' if record_count_ok else 'WARNING: COUNT MISMATCH'})"
        ),
        f"Columns after cleaning: {len(df.columns)}",
        f"Missing cells across all source columns: {missing_cells}",
        f"Missing county FIPS values: {int(missing_id.sum())}",
        f"Rows involved in duplicate county IDs: {int(duplicate_id.sum())}",
        f"Duplicate county IDs beyond first occurrence: {duplicate_count}",
        f"Prevalence columns checked: {len(prevalence_columns)}",
        f"Missing prevalence cells: {missing_prevalence_cells}",
        f"Out-of-range prevalence cells: {invalid_prevalence_cells}",
        f"Rows with out-of-range prevalence: {int(invalid_prevalence_rows.sum())}",
        f"Rows with one or more row-level flags: {int(df['has_quality_issue'].sum())}",
        "",
        "Missing values and flagged records are retained for review.",
        "Prevalence range checks assume percentage estimates from 0 to 100.",
        "Confidence-interval fields ending in _95ci remain text and are not parsed.",
        "",
        "Columns with the most missing values:",
    ]

    source_columns = [
        column for column in df.columns
        if column not in {"quality_flags", "has_quality_issue"}
    ]
    missing_by_column = (
        df[source_columns].isna().sum().sort_values(ascending=False)
    )
    for column, count in missing_by_column.head(10).items():
        report.append(f"  {column}: {count}")

    report_text = "\n".join(report)
    (OUTPUT_DIR / "quality_report.txt").write_text(
        report_text, encoding="utf-8"
    )
    print("\n" + report_text)
    return df, report_text


# --------------------------------------------------
# Summarize and visualize
# --------------------------------------------------

def summarize_data(df):
    """Summarize age-adjusted diabetes prevalence by Wisconsin county."""
    measure = "diabetes_adjprev"

    if measure not in df.columns:
        print(
            f"Required age-adjusted measure '{measure}' was not found. "
            "Check the current dataset schema; no chart was produced."
        )
        return None

    required = {"countyname", measure}
    if not required.issubset(df.columns):
        print("Required county or estimate columns are missing; no chart produced.")
        return None

    summary = df[["countyname", measure]].copy()
    summary = summary.rename(
        columns={measure: "age_adjusted_prevalence_percent"}
    )

    # Keep only rows with usable county names and valid percentage estimates.
    summary = summary.dropna(
        subset=["countyname", "age_adjusted_prevalence_percent"]
    )
    summary = summary[
        summary["age_adjusted_prevalence_percent"].between(0, 100)
    ]
    summary = summary.sort_values(
        "age_adjusted_prevalence_percent", ascending=False
    )

    summary.to_csv(
        OUTPUT_DIR / "wi_diabetes_age_adjusted_summary.csv",
        index=False,
    )

    if summary.empty:
        print("No valid age-adjusted diabetes estimates are available.")
        return None

    top = summary.head(15).sort_values(
        "age_adjusted_prevalence_percent"
    )

    plt.figure(figsize=(10, 7))
    plt.barh(
        top["countyname"],
        top["age_adjusted_prevalence_percent"],
    )
    plt.xlabel("Estimated age-adjusted prevalence (%)")
    plt.ylabel("County")
    plt.title(
        "Wisconsin Counties: Age-Adjusted Adult Diabetes Prevalence\n"
        "CDC PLACES, 2025 release"
    )
    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / "wi_diabetes_age_adjusted.png", dpi=150)
    plt.close()

    print("\nHighest age-adjusted diabetes prevalence estimates:")
    print(summary.head(10).to_string(index=False))

    return {
        "measure": measure,
        "summary": summary,
    }


# --------------------------------------------------
# README and data dictionary
# --------------------------------------------------

def create_readme(raw, metadata, report_text, summary_info):
    """Write the README at the repository root for GitHub display."""
    descriptions = {}

    for item in metadata:
        field = item.get("fieldName", "")
        if not field:
            continue
        key = standardize_column(field)
        descriptions[key] = {
            "name": item.get("name", field),
            "description": item.get("description", ""),
        }

    readme = [
        "# Wisconsin Public Health Data Collector",
        "",
        "## Overview",
        "",
        "This project retrieves CDC PLACES county-level public health estimates "
        "for Wisconsin through the Socrata API. It demonstrates API pagination, "
        "data cleaning, quality checks, summary analysis, visualization, and "
        "documentation.",
        "",
        "## Data source",
        "",
        f"**Dataset:** {DATASET_NAME}",
        "",
        "**Publisher:** Centers for Disease Control and Prevention (CDC)",
        "",
        f"**Dataset page:** {DATASET_PAGE}",
        "",
        f"**API endpoint:** {BASE_URL}",
        "",
        "**Geographic scope:** Wisconsin county records",
        "",
        "The CDC PLACES estimates are model-based population-level estimates, "
        "not exact counts of individual residents. Interpret differences with "
        "care and consult CDC measure definitions and methodology.",
        "",
        "## Run the project",
        "",
        "Install dependencies:",
        "",
        "```bash",
        "pip install requests pandas matplotlib",
        "```",
        "",
        "Run from the project root:",
        "",
        "```bash",
        "python wi_places_collector.py",
        "```",
        "",
        "## Outputs",
        "",
        "- `output/wi_places_raw.csv` — records as returned by the API.",
        "- `output/wi_places_clean.csv` — cleaned fields and row-level QA flags.",
        "- `output/quality_report.txt` — missingness, duplicate, range, and count checks.",
        "- `output/wi_diabetes_age_adjusted_summary.csv` — county estimates sorted high to low.",
        "- `output/wi_diabetes_age_adjusted.png` — chart of the 15 highest estimates.",
        "- `README.md` — project overview and data dictionary (this file).",
        "",
        "## Quality checks",
        "",
        "The script checks whether exactly 72 Wisconsin county records were "
        "retrieved, identifies missing and duplicate county FIPS values, counts "
        "missing values, and checks crude and age-adjusted prevalence estimates "
        "for values outside 0–100. Suspect rows are flagged rather than silently "
        "removed. The current run's findings are included below.",
        "",
        "```text",
        report_text,
        "```",
        "",
        "## Analysis choice",
        "",
        "The county ranking uses `diabetes_adjprev` (age-adjusted prevalence), "
        "rather than `diabetes_crudeprev` (crude prevalence). Crude prevalence "
        "can partly reflect differences in county age structure. Age adjustment "
        "helps make comparisons between populations with different age "
        "distributions more meaningful, though it does not remove every source "
        "of uncertainty or confounding.",
        "",
        "The chart shows the 15 counties with the highest valid age-adjusted "
        "estimates. This is a descriptive comparison, not evidence that small "
        "differences between counties are statistically meaningful.",
        "",
        "## Limitations",
        "",
        "- This project downloads Wisconsin records only.",
        "- CDC PLACES estimates are model-based and are subject to uncertainty.",
        "- Confidence-interval columns ending in `_95ci` contain text such as "
        "`(9.1, 10.2)`. They are preserved as text, not parsed, and are not used "
        "in the ranking or chart.",
        "- Missing values are reported rather than imputed.",
        "- Rows with detected issues are flagged rather than automatically deleted.",
        "- The prevalence range check assumes estimates are percentages from 0 to 100.",
        "- A ranking by point estimate alone does not establish statistically "
        "meaningful differences between counties.",
        "",
        "## Data dictionary",
        "",
        "Source fields below are documented using the dataset metadata retrieved "
        "from Socrata. Confidence-interval fields are text fields and are not "
        "used in the county ranking.",
        "",
        "| Field | Description |",
        "|---|---|",
    ]

    for column in raw.columns:
        normalized = standardize_column(column)
        details = descriptions.get(normalized, {})
        display_name = str(details.get("name") or column)
        description = str(
            details.get("description")
            or "Description not supplied in retrieved metadata."
        )
        display_name = display_name.replace("|", "\\|").replace("\n", " ")
        description = description.replace("|", "\\|").replace("\n", " ")
        readme.append(
            f"| `{normalized}` ({display_name}) | {description} |"
        )

    readme.extend([
        "",
        "Additional fields added during validation:",
        "",
        "- `quality_flags`: explanation of any detected row-level issues.",
        "- `has_quality_issue`: whether a row has one or more quality flags.",
        "",
    ])

    README_PATH.write_text("\n".join(readme), encoding="utf-8")
    print(f"README written to project root: {README_PATH.resolve()}")


# --------------------------------------------------
# Main
# --------------------------------------------------

def main():
    print("Retrieving dataset metadata...")
    metadata = get_metadata()

    print("\nDownloading Wisconsin public health data...")
    raw = download_data()

    print("\nCleaning data...")
    clean = clean_data(raw, metadata)

    print("\nValidating data...")
    clean, report_text = validate_data(clean)
    clean.to_csv(OUTPUT_DIR / "wi_places_clean.csv", index=False)

    print("\nCreating age-adjusted summary and chart...")
    summary_info = summarize_data(clean)

    print("\nGenerating root README and data dictionary...")
    create_readme(raw, metadata, report_text, summary_info)

    print("\nProject complete!")
    print(f"Outputs saved in: {OUTPUT_DIR.resolve()}")


if __name__ == "__main__":
    main()
