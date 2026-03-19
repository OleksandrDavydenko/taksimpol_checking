from __future__ import annotations

from pathlib import Path
import tempfile

import pandas as pd
import streamlit as st

from api_reader import TABLE_NAME
from index import build_api_mapping, compare_and_enrich
from api_reader import read_powerbi_table
from pdf_reader import extract_pdf_to_dataframe


DEFAULT_SCALE = 2.0
DEFAULT_ROTATION = 270
DEFAULT_AUTO_ROTATE = False
DEFAULT_PSM = 11


@st.cache_data(ttl=300, show_spinner=False)
def fetch_api_map(table_name: str) -> pd.DataFrame:
    api_df = read_powerbi_table(table_name)
    if api_df.empty:
        return pd.DataFrame()
    return build_api_mapping(api_df)


def analyze_pdf(
    pdf_bytes: bytes,
    table_name: str,
    scale: float,
    rotation: int,
    auto_rotate: bool,
    psm: int,
) -> tuple[pd.DataFrame, str | None]:
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(pdf_bytes)
            temp_path = Path(tmp.name)

        pdf_df = extract_pdf_to_dataframe(
            pdf_path=temp_path,
            scale=scale,
            rotation=rotation,
            auto_rotate=auto_rotate,
            psm=psm,
        )
        if pdf_df.empty:
            return pd.DataFrame(), "Не вдалося витягнути рядки з PDF."

        api_map = fetch_api_map(table_name)
        if api_map.empty:
            return pd.DataFrame(), "API не повернуло дані для звірки."

        result_df = compare_and_enrich(pdf_df, api_map)
        return result_df, None
    except Exception as error:
        return pd.DataFrame(), f"Помилка під час аналізу: {error}"
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)


def main() -> None:
    st.set_page_config(page_title="PDF vs API звірка", layout="wide")
    st.title("Звірка PDF з даними Power BI API")
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
                scale=DEFAULT_SCALE,
                rotation=DEFAULT_ROTATION,
                auto_rotate=DEFAULT_AUTO_ROTATE,
                psm=DEFAULT_PSM,
            )

        if error_message:
            st.error(error_message)
            return

        total_count = len(result_df)
        found_count = int(result_df["found_in_api"].sum())
        amount_match_count = int(result_df["amount_match"].sum())

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

        csv_data = result_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="Завантажити результат (CSV)",
            data=csv_data,
            file_name="pdf_vs_api_result.csv",
            mime="text/csv",
        )


if __name__ == "__main__":
    main()
