from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tempfile

import pandas as pd

from api_reader import normalize_column_name, read_powerbi_table
from pdf_reader import extract_pdf_to_dataframe


@dataclass(frozen=True)
class OcrSettings:
    scale: float = 2.0
    rotation: int = 270
    auto_rotate: bool = True
    psm: int = 11


DEFAULT_OCR_SETTINGS = OcrSettings()


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


def summarize_result(result_df: pd.DataFrame) -> dict[str, int]:
    total_count = len(result_df)
    found_count = int(result_df["found_in_api"].sum())
    amount_match_count = int(result_df["amount_match"].sum())
    return {
        "total": total_count,
        "found_in_api": found_count,
        "amount_match": amount_match_count,
    }


def run_reconciliation(
    pdf_path: Path,
    table_name: str | None = None,
    ocr: OcrSettings = DEFAULT_OCR_SETTINGS,
) -> pd.DataFrame:
    pdf_df = extract_pdf_to_dataframe(
        pdf_path=pdf_path,
        scale=ocr.scale,
        rotation=ocr.rotation,
        auto_rotate=ocr.auto_rotate,
        psm=ocr.psm,
    )
    if pdf_df.empty:
        return pd.DataFrame()

    api_df = read_powerbi_table(table_name) if table_name else read_powerbi_table()
    if api_df.empty:
        return pd.DataFrame()

    api_map = build_api_mapping(api_df)
    return compare_and_enrich(pdf_df, api_map)


def run_reconciliation_from_pdf_bytes(
    pdf_bytes: bytes,
    table_name: str,
    ocr: OcrSettings = DEFAULT_OCR_SETTINGS,
) -> pd.DataFrame:
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(pdf_bytes)
            temp_path = Path(tmp.name)
        return run_reconciliation(pdf_path=temp_path, table_name=table_name, ocr=ocr)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)
