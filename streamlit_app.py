from __future__ import annotations

from io import BytesIO

import pandas as pd
import streamlit as st

from api_reader import TABLE_NAME
from reconciliation import (
    DEFAULT_OCR_SETTINGS,
    run_reconciliation_from_pdf_bytes,
    summarize_result,
)


def build_excel_bytes(df: pd.DataFrame) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="result", index=False)
    return output.getvalue()


def analyze_pdf(
    pdf_bytes: bytes,
    table_name: str,
) -> tuple[pd.DataFrame, str | None]:
    try:
        result_df = run_reconciliation_from_pdf_bytes(
            pdf_bytes=pdf_bytes,
            table_name=table_name,
            ocr=DEFAULT_OCR_SETTINGS,
        )
        if result_df.empty:
            return (
                pd.DataFrame(),
                "Не вдалося витягнути рядки з PDF або API не повернуло дані для звірки.",
            )
        return result_df, None
    except Exception as error:
        return pd.DataFrame(), f"Помилка під час аналізу: {error}"


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
        st.dataframe(result_df, use_container_width=True, height=450)

        excel_data = build_excel_bytes(result_df)
        st.download_button(
            label="Завантажити результат (XLSX)",
            data=excel_data,
            file_name="pdf_vs_api_result.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


if __name__ == "__main__":
    main()
