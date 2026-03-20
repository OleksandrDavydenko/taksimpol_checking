from __future__ import annotations

import argparse
from pathlib import Path
import re
from statistics import median
import sys

import pandas as pd
import pypdfium2 as pdfium
import pytesseract
from pytesseract import Output


SETTLEMENT_LINE_PATTERN = re.compile(
    r"(?P<deal>\d{3,5}/C/\d{4}).*?"
    r"(?P<date>\d{2}\.\d{2}\.\d{4}).*?"
    r"(?P<amount>(?:\d{1,3}(?:[\s\u00A0,\.]\d{3})+|\d+)[\.,]\d{2}).*?"
    r"(?P<mawb>\d{3}\s+\d{4}\s+\d{4})"
)

SETTLEMENT_BLOCK_PATTERN = re.compile(
    r"(?P<deal>\d{3,5}/C/\d{4}).{0,120}?"
    r"(?P<date>\d{2}\.\d{2}\.\d{4}).{0,200}?"
    r"(?P<amount>[\[\(]?\s*(?:\d{1,3}(?:[\s\u00A0,\.]\d{3})+|\d+)[\.,]\s*\d{2}).{0,120}?"
    r"(?P<mawb>\d{3}\s+\d{4}\s+\d{4})",
    re.DOTALL,
)



def find_pdf_in_directory(directory: Path) -> Path:
    pdf_files = sorted(directory.glob("*.pdf"))
    if not pdf_files:
        raise FileNotFoundError("No PDF file found in the current directory.")
    return pdf_files[0]


def normalize_text(value: str) -> str:
    compact = re.sub(r"\s+", "", value.lower())
    compact = compact.replace("[", "(").replace("]", ")")
    compact = compact.replace("l", "1")
    return compact


def run_ocr(image, psm: int = 11) -> list[dict[str, float | str]]:
    data = pytesseract.image_to_data(
        image,
        output_type=Output.DICT,
        config=f"--oem 3 --psm {psm}",
        lang="eng",
        timeout=40,
    )
    tokens: list[dict[str, float | str]] = []

    for i, text in enumerate(data["text"]):
        raw = (text or "").strip()
        if not raw:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if conf < 0:
            continue

        x = float(data["left"][i])
        y = float(data["top"][i])
        w = float(data["width"][i])
        h = float(data["height"][i])

        tokens.append(
            {
                "text": raw,
                "norm": normalize_text(raw),
                "conf": conf,
                "x": x + w / 2,
                "y": y + h / 2,
                "x_left": x,
                "y_top": y,
            }
        )

    return tokens


def choose_best_orientation(
    image,
    rotation: int,
    auto_rotate: bool,
    psm: int,
) -> tuple[list[dict[str, float | str]], int]:
    primary = run_ocr(image.rotate(rotation, expand=True), psm=psm)
    if not auto_rotate:
        return primary, rotation

    rotations = [rotation, 0, 90, 180, 270]
    checked_rotations: list[int] = []
    candidates: list[tuple[int, int, list[dict[str, float | str]], int]] = []

    for candidate_rotation in rotations:
        if candidate_rotation in checked_rotations:
            continue
        checked_rotations.append(candidate_rotation)

        tokens = primary if candidate_rotation == rotation else run_ocr(
            image.rotate(candidate_rotation, expand=True),
            psm=psm,
        )

        inc_hits = sum(
            token["norm"] in {"inc(a)", "inca", "inc(a", "inc"}
            for token in tokens
        )
        mawb_hits = sum("mawb" in str(token["norm"]) for token in tokens)
        score = inc_hits + mawb_hits
        candidates.append((score, mawb_hits, tokens, candidate_rotation))

    # Prefer orientation with stronger header signal, especially MAWB hits.
    best = max(candidates, key=lambda item: (item[0], item[1]))
    return best[2], best[3]


def extract_inc_and_mawb_from_tokens(
    tokens: list[dict[str, float | str]], page_number: int
) -> list[dict[str, object]]:
    inc_candidates = [
        token
        for token in tokens
        if str(token["norm"]) in {"inc(a)", "inca", "inc(a", "inc"}
    ]
    mawb_candidates = [token for token in tokens if "mawb" in str(token["norm"])]

    if not inc_candidates or not mawb_candidates:
        return []

    x_inc = median(float(token["x"]) for token in inc_candidates)
    x_mawb = median(float(token["x"]) for token in mawb_candidates)
    header_y = median(
        [float(token["y"]) for token in inc_candidates + mawb_candidates]
    )

    col_distance = abs(x_inc - x_mawb)
    if col_distance < 20:
        return []

    x_tolerance = max(45.0, col_distance * 0.30)
    y_tolerance = 14.0
    rows: list[dict[str, object]] = []

    content_tokens = sorted(
        [token for token in tokens if float(token["y"]) > header_y + 6],
        key=lambda item: (float(item["y"]), float(item["x"])),
    )

    for token in content_tokens:
        x = float(token["x"])
        y = float(token["y"])
        text = str(token["text"])

        dist_inc = abs(x - x_inc)
        dist_mawb = abs(x - x_mawb)
        nearest = "inc" if dist_inc <= dist_mawb else "mawb"
        nearest_dist = min(dist_inc, dist_mawb)

        if nearest_dist > x_tolerance:
            continue

        row = next((row for row in rows if abs(float(row["y"]) - y) <= y_tolerance), None)
        if row is None:
            row = {"y": y, "inc_tokens": [], "mawb_tokens": []}
            rows.append(row)

        target = "inc_tokens" if nearest == "inc" else "mawb_tokens"
        row[target].append((x, text))

    extracted_rows: list[dict[str, object]] = []
    for row in sorted(rows, key=lambda item: float(item["y"])):
        inc_text = " ".join(text for _, text in sorted(row["inc_tokens"], key=lambda t: t[0]))
        mawb_text = " ".join(
            text for _, text in sorted(row["mawb_tokens"], key=lambda t: t[0])
        )

        # Support amounts like "2 549,00", "2,549.00", "2.549,00", and "549,00".
        inc_match = re.search(
            r"\d{1,3}(?:[\s\u00A0,\.]\d{3})+[\.,]\d{2}|\d+[\.,]\d{2}",
            inc_text,
        )
        if not inc_match:
            continue

        raw_amount = re.sub(r"\s+", "", inc_match.group(0))

        if "," in raw_amount and "." in raw_amount:
            decimal_sep = "," if raw_amount.rfind(",") > raw_amount.rfind(".") else "."
            thousands_sep = "." if decimal_sep == "," else ","
            normalized_amount = raw_amount.replace(thousands_sep, "")
            normalized_amount = normalized_amount.replace(decimal_sep, ".")
        elif "," in raw_amount:
            normalized_amount = raw_amount.replace(".", "")
            normalized_amount = normalized_amount.replace(",", ".")
        else:
            if raw_amount.count(".") > 1:
                parts = raw_amount.split(".")
                normalized_amount = "".join(parts[:-1]) + "." + parts[-1]
            else:
                normalized_amount = raw_amount

        inc_value = normalized_amount
        mawb_digits = re.sub(r"\D", "", mawb_text)
        if len(mawb_digits) != 11:
            continue

        mawb_value = mawb_digits

        extracted_rows.append(
            {
                "page": page_number,
                "inc(a)": inc_value,
                "MAWB": mawb_value,
            }
        )

    return extracted_rows


def extract_settlement_rows_from_text(text: str, page_number: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    text_upper = text.upper().replace("|", " ")
    text_upper = re.sub(r"([\.,]\d)\s+(\d)", r"\1\2", text_upper)
    seen_pairs: set[tuple[str, str]] = set()

    for line in text_upper.splitlines():
        match = SETTLEMENT_LINE_PATTERN.search(line)
        if not match:
            continue

        raw_amount = re.sub(r"\s+", "", match.group("amount").replace("[", "").replace("(", ""))
        if "," in raw_amount and "." in raw_amount:
            decimal_sep = "," if raw_amount.rfind(",") > raw_amount.rfind(".") else "."
            thousands_sep = "." if decimal_sep == "," else ","
            normalized_amount = raw_amount.replace(thousands_sep, "")
            normalized_amount = normalized_amount.replace(decimal_sep, ".")
        elif "," in raw_amount:
            normalized_amount = raw_amount.replace(".", "")
            normalized_amount = normalized_amount.replace(",", ".")
        else:
            if raw_amount.count(".") > 1:
                parts = raw_amount.split(".")
                normalized_amount = "".join(parts[:-1]) + "." + parts[-1]
            else:
                normalized_amount = raw_amount

        mawb_value = re.sub(r"\D", "", match.group("mawb"))
        if len(mawb_value) != 11:
            continue

        row_key = (normalized_amount, mawb_value)
        if row_key in seen_pairs:
            continue
        seen_pairs.add(row_key)

        rows.append(
            {
                "page": page_number,
                "inc(a)": normalized_amount,
                "MAWB": mawb_value,
            }
        )

    if rows:
        return rows

    for match in SETTLEMENT_BLOCK_PATTERN.finditer(text_upper):
        raw_amount = re.sub(r"\s+", "", match.group("amount").replace("[", "").replace("(", ""))
        if "," in raw_amount and "." in raw_amount:
            decimal_sep = "," if raw_amount.rfind(",") > raw_amount.rfind(".") else "."
            thousands_sep = "." if decimal_sep == "," else ","
            normalized_amount = raw_amount.replace(thousands_sep, "")
            normalized_amount = normalized_amount.replace(decimal_sep, ".")
        elif "," in raw_amount:
            normalized_amount = raw_amount.replace(".", "")
            normalized_amount = normalized_amount.replace(",", ".")
        else:
            if raw_amount.count(".") > 1:
                parts = raw_amount.split(".")
                normalized_amount = "".join(parts[:-1]) + "." + parts[-1]
            else:
                normalized_amount = raw_amount

        mawb_value = re.sub(r"\D", "", match.group("mawb"))
        if len(mawb_value) != 11:
            continue

        row_key = (normalized_amount, mawb_value)
        if row_key in seen_pairs:
            continue
        seen_pairs.add(row_key)

        rows.append(
            {
                "page": page_number,
                "inc(a)": normalized_amount,
                "MAWB": mawb_value,
            }
        )

    return rows


def extract_pdf_to_dataframe(
    pdf_path: Path,
    scale: float = 2.0,
    rotation: int = 270,
    auto_rotate: bool = False,
    psm: int = 11,
) -> pd.DataFrame:
    document = pdfium.PdfDocument(str(pdf_path))
    rows: list[dict[str, object]] = []
    detected_rotation = rotation

    for page_index in range(len(document)):
        page = document[page_index]
        image = page.render(scale=scale).to_pil()

        if auto_rotate and page_index == 0:
            tokens, detected_rotation = choose_best_orientation(
                image=image,
                rotation=rotation,
                auto_rotate=True,
                psm=psm,
            )
        else:
            tokens = run_ocr(
                image.rotate(detected_rotation, expand=True),
                psm=psm,
            )

        rows.extend(extract_inc_and_mawb_from_tokens(tokens, page_index + 1))

    if len(rows) < 2:
        # Fallback for settlement-style PDFs where row text is better recognized
        # as whole lines than as column headers/tokens.
        rows = []
        fallback_rotation = rotation
        fallback_scale = max(scale, 3.0)
        for page_index in range(len(document)):
            page = document[page_index]
            base_image = page.render(scale=fallback_scale).to_pil()

            rotations = [fallback_rotation]
            if page_index == 0:
                for candidate_rotation in (0, 90, 180, 270):
                    if candidate_rotation not in rotations:
                        rotations.append(candidate_rotation)

            page_rows: list[dict[str, object]] = []
            best_rotation = fallback_rotation
            for candidate_rotation in rotations:
                text = pytesseract.image_to_string(
                    base_image.rotate(candidate_rotation, expand=True),
                    lang="eng",
                    config="--oem 3 --psm 4",
                    timeout=40,
                )
                candidate_rows = extract_settlement_rows_from_text(text, page_index + 1)
                if len(candidate_rows) > len(page_rows):
                    page_rows = candidate_rows
                    best_rotation = candidate_rotation

            if not page_rows and page_index > 0:
                # Recovery path: if chosen rotation produced no rows on later pages,
                # quickly try other rotations for this page only.
                for candidate_rotation in (0, 90, 180, 270):
                    if candidate_rotation == fallback_rotation:
                        continue
                    text = pytesseract.image_to_string(
                        base_image.rotate(candidate_rotation, expand=True),
                        lang="eng",
                        config="--oem 3 --psm 4",
                        timeout=40,
                    )
                    candidate_rows = extract_settlement_rows_from_text(text, page_index + 1)
                    if len(candidate_rows) > len(page_rows):
                        page_rows = candidate_rows
                        best_rotation = candidate_rotation

            fallback_rotation = best_rotation

            rows.extend(page_rows)

    if not rows:
        return pd.DataFrame(columns=["inc(a)", "MAWB"])

    df = pd.DataFrame(rows)
    df = df[["inc(a)", "MAWB"]]
    df = df.replace("", pd.NA).dropna(subset=["inc(a)", "MAWB"])
    df["inc(a)"] = df["inc(a)"].astype(str).str.replace(",", ".", regex=False)
    df["inc(a)"] = pd.to_numeric(df["inc(a)"], errors="coerce")
    df = df.dropna(subset=["inc(a)"])
    df["inc(a)"] = df["inc(a)"].map(lambda value: f"{value:.2f}")
    df["MAWB"] = df["MAWB"].astype(str).str.replace(r"\D", "", regex=True)
    df = df[df["MAWB"].str.fullmatch(r"\d{11}")]
    return df.reset_index(drop=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read a PDF file and print its content as a DataFrame."
    )
    parser.add_argument(
        "--pdf",
        type=Path,
        help="Path to a PDF file. If omitted, the first PDF in the current directory is used.",
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

    df = extract_pdf_to_dataframe(
        pdf_path=pdf_path,
        scale=args.scale,
        rotation=args.rotation,
        auto_rotate=args.auto_rotate,
        psm=args.psm,
    )

    if df.empty:
        print("Could not extract columns inc(a) and MAWB from the PDF.")
        return 0

    print(df.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())