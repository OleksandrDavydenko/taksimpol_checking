from __future__ import annotations

from io import BytesIO
from pathlib import Path
import tempfile

import pandas as pd
import requests
import streamlit as st

from api_reader import TABLE_NAME, read_powerbi_table
from pdf_reader import extract_pdf_to_dataframe
from reconciliation import (
    DEFAULT_OCR_SETTINGS,
    build_api_mapping,
    compare_and_enrich,
    summarize_result,
)


def dataframe_height_for_rows(row_count: int) -> int:
    # Keep table compact for small results and scrollable for large ones.
    header_px = 38
    row_px = 35
    min_height = 140
    max_height = 450
    calculated = header_px + max(row_count, 1) * row_px
    return max(min_height, min(max_height, calculated))


def build_excel_bytes(df: pd.DataFrame) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="result", index=False)
    return output.getvalue()


def analyze_pdf(
    pdf_bytes: bytes,
    table_name: str,
) -> tuple[pd.DataFrame, str | None]:
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(pdf_bytes)
            temp_path = Path(tmp.name)

        pdf_df = extract_pdf_to_dataframe(
            pdf_path=temp_path,
            scale=DEFAULT_OCR_SETTINGS.scale,
            rotation=DEFAULT_OCR_SETTINGS.rotation,
            auto_rotate=DEFAULT_OCR_SETTINGS.auto_rotate,
            psm=DEFAULT_OCR_SETTINGS.psm,
        )
        if pdf_df.empty:
            return (
                pd.DataFrame(),
                "Не вдалося витягнути рядки з PDF. Перевірте формат файлу або якість скану.",
            )

        try:
            api_df = read_powerbi_table(table_name)
        except RuntimeError as error:
            return pd.DataFrame(), f"Помилка налаштування API: {error}"
        except requests.HTTPError:
            return (
                pd.DataFrame(),
                "Помилка запиту до Power BI API (авторизація або доступ до dataset).",
            )
        except requests.RequestException as error:
            return pd.DataFrame(), f"Помилка мережі при зверненні до API: {error}"

        if api_df.empty:
            return pd.DataFrame(), "Power BI API повернуло 0 рядків для звірки."

        try:
            api_map = build_api_mapping(api_df)
        except ValueError as error:
            return pd.DataFrame(), f"Некоректна структура даних API: {error}"

        result_df = compare_and_enrich(pdf_df, api_map)
        return result_df, None
    except Exception as error:
        return pd.DataFrame(), f"Помилка під час аналізу: {error}"
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)


def main() -> None:
    st.set_page_config(page_title="PDF vs API звірка", layout="wide")
    st.title("Звірка TAKSIMPOL з даними обліку")
    st.caption("Завантажте PDF, програма витягне MAWB/суму, звірить з API та покаже результат.")

    uploaded_file = st.file_uploader("PDF файл", type=["pdf"])

    if uploaded_file is None:
        st.info("Оберіть PDF файл для запуску звірки.")
        return

    if st.button("Запустити звірку", type="primary"):
        with st.spinner("Обробляю PDF та звіряю з API..."):
            result_df, error_message = analyze_pdf(
                pdf_bytes=uploaded_file.getvalue(),
                table_name=TABLE_NAME,
            )

        if error_message:
            st.error(error_message)
            return

        summary = summarize_result(result_df)
        total_count = summary["total"]
        found_count = summary["found_in_api"]
        amount_match_count = summary["amount_match"]

        col1, col2, col3 = st.columns(3)
        col1.metric("Всього рядків", total_count)
        col2.metric("Знайдено MAWB в API", f"{found_count}/{total_count}")
        col3.metric("Збіг сум", f"{amount_match_count}/{total_count}")

        mismatch_df = result_df[(~result_df["found_in_api"]) | (~result_df["amount_match"])].copy()

        st.subheader("Розбіжності")
        if mismatch_df.empty:
            st.success("Розбіжностей не знайдено.")
        else:
            st.dataframe(mismatch_df, use_container_width=True)

        st.subheader("Повний результат")
        st.dataframe(
            result_df,
            use_container_width=True,
            height=dataframe_height_for_rows(len(result_df)),
        )

        excel_data = build_excel_bytes(result_df)
        st.download_button(
            label="Завантажити результат (XLSX)",
            data=excel_data,
            file_name="pdf_vs_api_result.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


if __name__ == "__main__":
    main()
