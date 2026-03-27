from __future__ import annotations

from io import BytesIO
from pathlib import Path
import tempfile
from typing import Callable

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
    progress_cb: Callable[[str], None] | None = None,
) -> tuple[pd.DataFrame, str | None]:
    def report(message: str) -> None:
        if progress_cb:
            progress_cb(message)

    temp_path: Path | None = None
    try:
        report("Підготовка вхідного PDF файлу...")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(pdf_bytes)
            temp_path = Path(tmp.name)

        report("Обробка PDF (OCR та витяг рядків)...")
        pdf_df = extract_pdf_to_dataframe(
            pdf_path=temp_path,
            scale=DEFAULT_OCR_SETTINGS.scale,
            rotation=DEFAULT_OCR_SETTINGS.rotation,
            auto_rotate=DEFAULT_OCR_SETTINGS.auto_rotate,
            psm=DEFAULT_OCR_SETTINGS.psm,
            progress_cb=report,
        )
        report(f"OCR: витягнуто рядків для звірки: {len(pdf_df)}")
        if pdf_df.empty:
            return (
                pd.DataFrame(),
                "Не вдалося витягнути рядки з PDF. Перевірте формат файлу або якість скану.",
            )

        try:
            report("Виконую запит до Power BI API...")
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
            report("Підготовка даних API для звірки...")
            api_map = build_api_mapping(api_df)
        except ValueError as error:
            return pd.DataFrame(), f"Некоректна структура даних API: {error}"

        report("Порівнюю дані PDF з API...")
        result_df = compare_and_enrich(pdf_df, api_map)
        report("Формую фінальну таблицю результатів...")
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

    if "is_processing" not in st.session_state:
        st.session_state["is_processing"] = False
    if "last_result_df" not in st.session_state:
        st.session_state["last_result_df"] = None
    if "last_error_message" not in st.session_state:
        st.session_state["last_error_message"] = None
    if "pending_pdf_bytes" not in st.session_state:
        st.session_state["pending_pdf_bytes"] = None

    uploaded_file = st.file_uploader("PDF файл", type=["pdf"])
    result_slot = st.empty()

    if uploaded_file is None:
        st.info("Оберіть PDF файл для запуску звірки.")
        return

    run_clicked = st.button(
        "Запустити звірку",
        type="primary",
        disabled=bool(st.session_state["is_processing"]),
    )

    if run_clicked and not st.session_state["is_processing"]:
        st.session_state["is_processing"] = True
        st.session_state["last_result_df"] = None
        st.session_state["last_error_message"] = None
        st.session_state["pending_pdf_bytes"] = uploaded_file.getvalue()
        st.rerun()

    if st.session_state["is_processing"]:
        # Clear previously rendered result tables while a new run is in progress.
        result_slot.empty()
        with st.status("Запуск процесу звірки...", expanded=True) as status:
            status.write("Перевіряю та готую вхідні дані...")

            def report_step(message: str) -> None:
                status.write(message)

            result_df, error_message = analyze_pdf(
                pdf_bytes=st.session_state["pending_pdf_bytes"] or uploaded_file.getvalue(),
                table_name=TABLE_NAME,
                progress_cb=report_step,
            )

            if error_message:
                status.update(label="Звірка завершилась з помилкою.", state="error")
            else:
                status.update(label="Звірку успішно завершено.", state="complete")

        st.session_state["last_result_df"] = result_df
        st.session_state["last_error_message"] = error_message
        st.session_state["is_processing"] = False
        st.session_state["pending_pdf_bytes"] = None
        st.rerun()

    result_df = st.session_state["last_result_df"]
    error_message = st.session_state["last_error_message"]

    if error_message:
        st.error(error_message)
        return

    if isinstance(result_df, pd.DataFrame) and not result_df.empty:
        with result_slot.container():
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
