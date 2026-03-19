from __future__ import annotations

import argparse
from pathlib import Path
import sys

import pandas as pd

from api_reader import (
    normalize_column_name,
    read_powerbi_table,
)
from pdf_reader import extract_pdf_to_dataframe, find_pdf_in_directory


def find_column_by_normalized_name(df: pd.DataFrame, expected_name: str) -> str | None:
    expected = normalize_column_name(expected_name)
    for column in df.columns:
        if normalize_column_name(str(column)) == expected:
            return str(column)
    return None


def build_api_mapping(api_df: pd.DataFrame) -> pd.DataFrame:
    mawb_column = find_column_by_normalized_name(api_df, "MAWB")
    deal_column = find_column_by_normalized_name(api_df, "ugoda")
    amount_column = find_column_by_normalized_name(api_df, "SUM_USD")

    if mawb_column is None:
        raise ValueError("Column 'MAWB' was not found in API data.")
    if deal_column is None:
        raise ValueError("Column 'ugoda' was not found in API data.")
    if amount_column is None:
        raise ValueError("Column 'SUM_USD' was not found in API data.")

    mapping = api_df[[deal_column, mawb_column, amount_column]].copy()
    mapping = mapping.rename(
        columns={
            deal_column: "Сделка",
            mawb_column: "MAWB",
            amount_column: "api_amount",
        }
    )
    mapping["Сделка"] = mapping["Сделка"].astype(str).str.strip()
    mapping["MAWB"] = mapping["MAWB"].astype(str).str.replace(r"\D", "", regex=True)
    mapping["api_amount"] = pd.to_numeric(mapping["api_amount"], errors="coerce")
    mapping = mapping[mapping["MAWB"].str.fullmatch(r"\d{11}")]
    mapping = mapping.drop_duplicates(subset=["MAWB"], keep="first")
    return mapping.reset_index(drop=True)


def compare_and_enrich(pdf_df: pd.DataFrame, api_map: pd.DataFrame) -> pd.DataFrame:
    merged = pdf_df.merge(api_map, on="MAWB", how="left")
    merged["pdf_amount"] = pd.to_numeric(merged["inc(a)"], errors="coerce")
    merged["found_in_api"] = merged["Сделка"].notna()
    merged["difference"] = merged["pdf_amount"] - merged["api_amount"]
    merged["difference"] = merged["difference"].round(2)
    merged["amount_match"] = merged["difference"].fillna(0).abs() <= 0.01
    return merged[
        [
            "inc(a)",
            "pdf_amount",
            "api_amount",
            "difference",
            "MAWB",
            "Сделка",
            "found_in_api",
            "amount_match",
        ]
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build DataFrame from PDF, fetch rows from Power BI API, compare by MAWB, "
            "and add Deal (Сделка) from API."
        )
    )
    parser.add_argument("--pdf", type=Path, help="Path to PDF file.")
    parser.add_argument(
        "--table",
        default=None,
        help="Power BI table name. If omitted, value from env PBI_TABLE is used.",
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=2.0,
        help="PDF render scale for OCR (lower is faster, default: 2.0).",
    )
    parser.add_argument(
        "--rotation",
        type=int,
        default=270,
        choices=[0, 90, 180, 270],
        help="Page rotation before OCR (default: 270).",
    )
    parser.add_argument(
        "--auto-rotate",
        action="store_true",
        help="Try fallback rotation if headers are not detected (slower).",
    )
    parser.add_argument(
        "--psm",
        type=int,
        default=11,
        help="Tesseract PSM mode (default: 11).",
    )
    args = parser.parse_args()

    pdf_path = args.pdf or find_pdf_in_directory(Path.cwd())

    if not pdf_path.exists():
        print(f"PDF file not found: {pdf_path}", file=sys.stderr)
        return 1

    pdf_df = extract_pdf_to_dataframe(
        pdf_path=pdf_path,
        scale=args.scale,
        rotation=args.rotation,
        auto_rotate=args.auto_rotate,
        psm=args.psm,
    )
    if pdf_df.empty:
        print("Could not extract rows from PDF.")
        return 0

    api_df = read_powerbi_table(args.table) if args.table else read_powerbi_table()
    if api_df.empty:
        print("No rows returned from API.")
        return 0

    try:
        api_map = build_api_mapping(api_df)
    except ValueError as error:
        print(str(error))
        return 0

    result_df = compare_and_enrich(pdf_df, api_map)

    matched_count = int(result_df["found_in_api"].sum())
    amount_match_count = int(result_df["amount_match"].sum())
    total_count = len(result_df)
    print(f"Matched by MAWB (API): {matched_count}/{total_count}")
    print(f"Matched by amount (PDF vs API): {amount_match_count}/{total_count}")
    try:
        print(result_df.to_string(index=False))
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())