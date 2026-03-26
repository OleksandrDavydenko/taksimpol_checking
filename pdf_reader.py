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
    r"(?P<date>\d{1,2}\.\d{2}\.\d{4}).*?"
    r"(?P<amount>(?:\d{1,3}(?:[\s\u00A0,\.]\d{3})+|\d+)[\.,]\d{2}).*?"
    r"(?P<mawb>\d{3}\s+\d{4}\s+\d{4})?"
)

SETTLEMENT_BLOCK_PATTERN = re.compile(
    r"(?P<deal>\d{3,5}/C/\d{4}).{0,120}?"
    r"(?P<date>\d{1,2}\.\d{2}\.\d{4}).{0,200}?"
    r"(?P<amount>[\[\(]?\s*(?:\d{1,3}(?:[\s\u00A0,\.]\d{3})+|\d+)[\.,]\s*\d{2})(?:.{0,120}?(?P<mawb>\d{3}\s+\d{4}\s+\d{4}))?",
    re.DOTALL,
)

DIGIT_MAWB_PATTERN = re.compile(r"\b\d{3}\s+\d{4}\s+\d{4}\b")
PARTIAL_DIGIT_MAWB_PATTERN = re.compile(r"\b\d{2}\s+\d{4}\s+\d{4}\b")
ALNUM_MAWB_PATTERN = re.compile(r"\b[A-Z]{2,5}\s+[A-Z0-9]{2,8}\s+\d{3,6}\b")
TABLE_ROW_PATTERN = re.compile(
    r"(?P<amount>\d{1,3}(?:[\s\u00A0,\.]\d{3})*[\.,]\d{2}).{0,100}?"
    r"(?P<mawb>(?:\d{3}\s+\d{4}\s+\d{4}|[A-Z]{2,5}\s+[A-Z0-9]{2,8}\s+\d{3,6}))"
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


def normalize_mawb(value: str) -> str:
    raw = (value or "").upper()
    return re.sub(r"[^A-Z0-9]", "", raw)


def extract_mawb_from_text(value: str) -> str:
    upper = (value or "").upper()
    digit_match = DIGIT_MAWB_PATTERN.search(upper)
    if digit_match:
        return normalize_mawb(digit_match.group(0))

    partial_digit_match = PARTIAL_DIGIT_MAWB_PATTERN.search(upper)
    if partial_digit_match:
        return normalize_mawb(partial_digit_match.group(0))

    alnum_match = ALNUM_MAWB_PATTERN.search(upper)
    if alnum_match:
        return normalize_mawb(alnum_match.group(0))

    return ""


def extract_digit_mawb_from_text(value: str) -> str:
    upper = (value or "").upper()
    digit_match = DIGIT_MAWB_PATTERN.search(upper)
    if digit_match:
        return normalize_mawb(digit_match.group(0))
    return ""


def normalize_amount_text(raw_amount: str) -> str:
    original = (raw_amount or "").strip()
    bleed_match = re.fullmatch(r"(\d{2})\s+(\d{3}[\.,]\d{2})", original)
    if bleed_match and 20 <= int(bleed_match.group(1)) <= 39:
        original = bleed_match.group(2)

    raw_amount = re.sub(r"\s+", "", original)
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
    return normalized_amount


def is_date_like_amount(value: str) -> bool:
    # Guard against OCR confusion where date fragments (e.g. 10.07) are read as amount.
    m = re.fullmatch(r"(\d{1,2})\.(\d{2})", (value or "").strip())
    if not m:
        return False
    left = int(m.group(1))
    right = int(m.group(2))
    return 1 <= left <= 31 and 1 <= right <= 12


def row_quality(rows: list[dict[str, object]]) -> tuple[int, int, int, int]:
    mawb_filled = sum(1 for row in rows if str(row.get("MAWB", "")).strip())
    valid_11 = sum(
        1
        for row in rows
        if re.fullmatch(r"\d{11}", str(row.get("MAWB", "")).strip() or "")
    )
    partial_10 = sum(
        1
        for row in rows
        if re.fullmatch(r"\d{10}", str(row.get("MAWB", "")).strip() or "")
    )
    # Prefer rows with more fully recognized 11-digit MAWB values.
    return valid_11, mawb_filled - partial_10, mawb_filled, len(rows)


def dataframe_quality(df: pd.DataFrame) -> tuple[int, int, int, int]:
    if df.empty:
        return (0, 0, 0, 0)
    date_like = int(df["inc(a)"].astype(str).map(is_date_like_amount).sum())
    mawb_filled = int(df["MAWB"].astype(str).str.strip().ne("").sum())
    non_date = len(df) - date_like
    return (non_date, mawb_filled, -date_like, len(df))


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
        mawb_value = extract_mawb_from_text(mawb_text)

        if not mawb_value:
            continue

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
    # Join OCR-broken amounts like "952\n.00" or "952 \n ,00".
    text_upper = re.sub(r"(\d)\s+([\.,]\s*\d{2})", r"\1\2", text_upper)
    text_upper = re.sub(r"([\.,]\d)\s+(\d)", r"\1\2", text_upper)
    seen_pairs: set[tuple[str, str]] = set()
    total_amounts: set[str] = set()

    for line in text_upper.splitlines():
        if not re.search(r"\b(RAZEM\w*|TOTAL|SUMA)\b", line):
            continue
        for amount_match in re.finditer(
            r"\d{1,3}(?:[\s\u00A0,\.]\d{3})*[\.,]\d{2}",
            line,
        ):
            raw_total = re.sub(r"\s+", "", amount_match.group(0))
            if "," in raw_total and "." in raw_total:
                decimal_sep = "," if raw_total.rfind(",") > raw_total.rfind(".") else "."
                thousands_sep = "." if decimal_sep == "," else ","
                normalized_total = raw_total.replace(thousands_sep, "")
                normalized_total = normalized_total.replace(decimal_sep, ".")
            elif "," in raw_total:
                normalized_total = raw_total.replace(".", "")
                normalized_total = normalized_total.replace(",", ".")
            else:
                if raw_total.count(".") > 1:
                    parts = raw_total.split(".")
                    normalized_total = "".join(parts[:-1]) + "." + parts[-1]
                else:
                    normalized_total = raw_total
            total_amounts.add(normalized_total)

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

        mawb_group = match.group("mawb") or ""
        mawb_value = extract_mawb_from_text(mawb_group) if mawb_group else extract_digit_mawb_from_text(line)
        if not mawb_value and is_date_like_amount(normalized_amount):
            continue
        if not mawb_value and normalized_amount in total_amounts:
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

        mawb_group = match.group("mawb") or ""
        mawb_value = extract_mawb_from_text(mawb_group) if mawb_group else extract_digit_mawb_from_text(match.group(0))
        if not mawb_value and is_date_like_amount(normalized_amount):
            continue
        if not mawb_value and normalized_amount in total_amounts:
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


def extract_table_rows_from_text(text: str, page_number: int) -> list[dict[str, object]]:
    text_upper = text.upper().replace("|", " ")
    rows: list[dict[str, object]] = []
    seen_pairs: set[tuple[str, str]] = set()
    amount_pattern = re.compile(r"\d{1,3}(?:[\s\u00A0,\.]\d{3})*[\.,]\d{2}")

    for line in text_upper.splitlines():
        if "RAZEM" in line or "TOTAL" in line or "SUMA" in line:
            continue
        if not re.search(r"\d{3,5}/C/\d{4}", line):
            continue

        date_matches = list(re.finditer(r"\d{1,2}\.\d{2}\.\d{4}", line))
        post_date_segment = line[date_matches[-1].end():] if date_matches else line
        post_date_segment = re.sub(r"\d{3,5}\s*/\s*C\s*/\s*\d{4}", " ", post_date_segment)
        base_amounts = [normalize_amount_text(m.group(0)) for m in amount_pattern.finditer(post_date_segment)]
        base_amounts = [value for value in base_amounts if not is_date_like_amount(value)]

        mawb_candidates = (
            list(DIGIT_MAWB_PATTERN.finditer(line))
            + list(PARTIAL_DIGIT_MAWB_PATTERN.finditer(line))
            + list(ALNUM_MAWB_PATTERN.finditer(line))
        )
        if mawb_candidates:
            for mawb_match in sorted(mawb_candidates, key=lambda m: m.start()):
                mawb = extract_mawb_from_text(mawb_match.group(0))
                if not mawb:
                    continue

                prefix = line[:mawb_match.start()]
                prefix_dates = list(re.finditer(r"\d{1,2}\.\d{2}\.\d{4}", prefix))
                amount_segment = prefix[prefix_dates[-1].end():] if prefix_dates else prefix
                amount_segment = re.sub(r"\d{3,5}\s*/\s*C\s*/\s*\d{4}", " ", amount_segment)

                amounts = [normalize_amount_text(m.group(0)) for m in amount_pattern.finditer(amount_segment)]
                amounts = [value for value in amounts if not is_date_like_amount(value)]

                if not amounts:
                    # OCR fallback: amount can lose decimal separator, e.g. "5 94" instead of "594.00".
                    split_match = re.match(r"\s*(\d)\s+(\d{2,3})\b", amount_segment)
                    if split_match:
                        candidate = f"{split_match.group(1)}{split_match.group(2)}.00"
                        amounts = [candidate]

                if not amounts:
                    # Last resort: pick first integer-like amount right after date.
                    int_match = re.match(r"\s*(\d{2,4})\b", amount_segment)
                    if int_match:
                        amounts = [f"{int_match.group(1)}.00"]

                if not amounts:
                    continue

                amount = amounts[-1]
                key = (amount, mawb)
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                rows.append({"page": page_number, "inc(a)": amount, "MAWB": mawb})
        else:
            if not date_matches:
                continue
            if not base_amounts:
                continue
            amount = base_amounts[0]
            key = (amount, "")
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            rows.append({"page": page_number, "inc(a)": amount, "MAWB": ""})

    return rows


def prepare_extracted_dataframe(rows: list[dict[str, object]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["inc(a)", "MAWB"])

    df = pd.DataFrame(rows)
    if "page" not in df.columns:
        df["page"] = 0
    df = df[["page", "inc(a)", "MAWB"]]
    df = df.replace("", pd.NA)
    df = df.dropna(subset=["inc(a)"])
    if df.empty:
        return pd.DataFrame(columns=["inc(a)", "MAWB"])

    df["inc(a)"] = df["inc(a)"].astype(str).str.replace(",", ".", regex=False)
    df["inc(a)"] = pd.to_numeric(df["inc(a)"], errors="coerce")
    df = df.dropna(subset=["inc(a)"])
    if df.empty:
        return pd.DataFrame(columns=["inc(a)", "MAWB"])

    # OCR can prepend "25" from year 2025 to values (e.g. 423.00 -> 25423.00).
    year_bleed_mask = (df["inc(a)"] >= 25000.0) & (df["inc(a)"] < 26000.0)
    if year_bleed_mask.any():
        df.loc[year_bleed_mask, "inc(a)"] = df.loc[year_bleed_mask, "inc(a)"] - 25000.0

    # Remove summary-total rows (e.g. "razem") that can be OCR'd as regular data rows.
    if len(df) >= 8:
        total_like_indexes: list[int] = []
        for idx in df.index[df["MAWB"].fillna("").eq("")]:
            current_value = float(df.at[idx, "inc(a)"])
            others_sum = float(df.loc[df.index != idx, "inc(a)"].sum())
            if abs(others_sum - current_value) <= 0.05:
                total_like_indexes.append(idx)
        if total_like_indexes:
            df = df.drop(index=total_like_indexes)

    # Safety net: drop a likely total row when it has empty MAWB and the amount
    # is an extreme outlier compared to the next largest extracted amount.
    if len(df) >= 8:
        empty_mawb_df = df[df["MAWB"].fillna("").eq("")]
        if not empty_mawb_df.empty:
            sorted_amounts = df["inc(a)"].sort_values(ascending=False).tolist()
            if len(sorted_amounts) >= 2:
                max_amount = float(sorted_amounts[0])
                second_max = float(sorted_amounts[1])
                if second_max > 0 and max_amount >= second_max * 2.5:
                    df = df[~(df["MAWB"].fillna("").eq("") & (df["inc(a)"] == max_amount))]

    df["inc(a)"] = df["inc(a)"].map(lambda value: f"{value:.2f}")
    df["MAWB"] = df["MAWB"].fillna("").astype(str).map(normalize_mawb)
    # Keep 10-digit MAWB values as OCR-partial identifiers for visibility in output.
    # They may not match API, but are useful for manual verification.
    df = df[df["MAWB"].eq("") | df["MAWB"].str.fullmatch(r"\d{10,11}|[A-Z0-9]{8,20}")]
    if df.empty:
        return pd.DataFrame(columns=["inc(a)", "MAWB"])

    # If for the same page+amount we have both noisy alnum MAWB and a clean 11-digit MAWB,
    # keep the clean one.
    group_key = df["page"].astype(str) + "|" + df["inc(a)"]
    has_digit_mawb = group_key[df["MAWB"].str.fullmatch(r"\d{11}")]
    has_digit_mawb_set = set(has_digit_mawb.tolist())
    drop_noisy_mask = (
        group_key.isin(has_digit_mawb_set)
        & ~df["MAWB"].str.fullmatch(r"\d{11}")
        & df["MAWB"].ne("")
    )
    df = df[~drop_noisy_mask]

    # Remove date-like pseudo-amounts caused by OCR column bleed.
    df = df[~df["inc(a)"].astype(str).map(is_date_like_amount)]
    if df.empty:
        return pd.DataFrame(columns=["inc(a)", "MAWB"])

    key = df["page"].astype(str) + "|" + df["inc(a)"]
    keys_with_mawb = set(key[df["MAWB"].ne("")].tolist())
    df = df[~(df["MAWB"].eq("") & key.isin(keys_with_mawb))]
    df = df.drop_duplicates(subset=["page", "MAWB", "inc(a)"], keep="first")
    return df[["inc(a)", "MAWB"]].reset_index(drop=True)


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

    primary_rows = rows

    # Fallback for settlement-style PDFs where row text is better recognized
    # as whole lines than as column headers/tokens.
    fallback_rows: list[dict[str, object]] = []
    if auto_rotate or len(primary_rows) < 8:
        fallback_rotation = rotation
        fallback_scales = sorted({max(scale, 2.5), max(scale, 3.0)})
        for page_index in range(len(document)):
            page = document[page_index]
            page_rows: list[dict[str, object]] = []
            best_rotation = fallback_rotation
            for fallback_scale in fallback_scales:
                base_image = page.render(scale=fallback_scale).to_pil()

                rotations = [fallback_rotation]
                if page_index == 0:
                    for candidate_rotation in (0, 90, 180, 270):
                        if candidate_rotation not in rotations:
                            rotations.append(candidate_rotation)

                for candidate_rotation in rotations:
                    rotated = base_image.rotate(candidate_rotation, expand=True)
                    candidate_rows: list[dict[str, object]] = []
                    for fallback_psm in (4, 6):
                        text = pytesseract.image_to_string(
                            rotated,
                            lang="eng",
                            config=f"--oem 3 --psm {fallback_psm}",
                            timeout=40,
                        )
                        settlement_rows = extract_settlement_rows_from_text(text, page_index + 1)
                        table_rows = extract_table_rows_from_text(text, page_index + 1)
                        parsed_rows = table_rows if row_quality(table_rows) > row_quality(settlement_rows) else settlement_rows
                        if row_quality(parsed_rows) > row_quality(candidate_rows):
                            candidate_rows = parsed_rows
                    if row_quality(candidate_rows) > row_quality(page_rows):
                        page_rows = candidate_rows
                        best_rotation = candidate_rotation

            if not page_rows and page_index > 0:
                # Recovery path: if chosen rotation produced no rows on later pages,
                # quickly try other rotations for this page only.
                for candidate_rotation in (0, 90, 180, 270):
                    if candidate_rotation == fallback_rotation:
                        continue
                    rotated = base_image.rotate(candidate_rotation, expand=True)
                    candidate_rows: list[dict[str, object]] = []
                    for fallback_psm in (4, 6):
                        text = pytesseract.image_to_string(
                            rotated,
                            lang="eng",
                            config=f"--oem 3 --psm {fallback_psm}",
                            timeout=40,
                        )
                        settlement_rows = extract_settlement_rows_from_text(text, page_index + 1)
                        table_rows = extract_table_rows_from_text(text, page_index + 1)
                        parsed_rows = table_rows if row_quality(table_rows) > row_quality(settlement_rows) else settlement_rows
                        if row_quality(parsed_rows) > row_quality(candidate_rows):
                            candidate_rows = parsed_rows
                    if row_quality(candidate_rows) > row_quality(page_rows):
                        page_rows = candidate_rows
                        best_rotation = candidate_rotation

            fallback_rotation = best_rotation

            fallback_rows.extend(page_rows)

    primary_df = prepare_extracted_dataframe(primary_rows)
    fallback_df = prepare_extracted_dataframe(fallback_rows)

    primary_score = dataframe_quality(primary_df)
    fallback_score = dataframe_quality(fallback_df)
    selected_df = fallback_df if fallback_score > primary_score else primary_df
    other_df = primary_df if fallback_score > primary_score else fallback_df

    if not selected_df.empty and not other_df.empty:
        # If selected OCR variant has empty MAWB for an amount, recover it from
        # the alternative variant when there is exactly one clear candidate.
        selected_df = selected_df.copy()
        other_non_empty = other_df[other_df["MAWB"].ne("")]
        if not other_non_empty.empty:
            amount_to_unique_mawb = (
                other_non_empty.groupby("inc(a)")["MAWB"]
                .agg(lambda values: sorted(set(values)))
                .to_dict()
            )
            empty_mask = selected_df["MAWB"].eq("")
            for idx in selected_df[empty_mask].index:
                amount = selected_df.at[idx, "inc(a)"]
                candidates = amount_to_unique_mawb.get(amount, [])
                if len(candidates) == 1:
                    selected_df.at[idx, "MAWB"] = candidates[0]

    if not other_df.empty:
        missing_empty_rows = other_df[
            other_df["MAWB"].eq("")
            & ~other_df["inc(a)"].isin(selected_df["inc(a)"])
        ]
        if not missing_empty_rows.empty:
            amount_values = pd.to_numeric(missing_empty_rows["inc(a)"], errors="coerce")
            missing_empty_rows = missing_empty_rows[amount_values >= 100.0]
        if not missing_empty_rows.empty:
            selected_df = pd.concat([selected_df, missing_empty_rows], ignore_index=True)
            selected_df = selected_df.drop_duplicates(subset=["inc(a)", "MAWB"], keep="first")

    return selected_df.reset_index(drop=True)


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