from __future__ import annotations

import argparse
from pathlib import Path
import sys

from pdf_reader import find_pdf_in_directory
from reconciliation import DEFAULT_OCR_SETTINGS, OcrSettings, run_reconciliation, summarize_result


def parse_args() -> argparse.Namespace:
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
        default=DEFAULT_OCR_SETTINGS.scale,
        help="PDF render scale for OCR (lower is faster, default: 2.0).",
    )
    parser.add_argument(
        "--rotation",
        type=int,
        default=DEFAULT_OCR_SETTINGS.rotation,
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
        default=DEFAULT_OCR_SETTINGS.psm,
        help="Tesseract PSM mode (default: 11).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    pdf_path = args.pdf or find_pdf_in_directory(Path.cwd())

    if not pdf_path.exists():
        print(f"PDF file not found: {pdf_path}", file=sys.stderr)
        return 1

    ocr = OcrSettings(
        scale=args.scale,
        rotation=args.rotation,
        auto_rotate=args.auto_rotate,
        psm=args.psm,
    )

    try:
        result_df = run_reconciliation(pdf_path=pdf_path, table_name=args.table, ocr=ocr)
    except ValueError as error:
        print(str(error))
        return 0

    if result_df.empty:
        print("Could not extract rows from PDF or API returned no data.")
        return 0

    summary = summarize_result(result_df)
    print(f"Matched by MAWB (API): {summary['found_in_api']}/{summary['total']}")
    print(f"Matched by amount (PDF vs API): {summary['amount_match']}/{summary['total']}")

    try:
        print(result_df.to_string(index=False))
    except BrokenPipeError:
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
