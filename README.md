# taksimpol_checking

Compare values extracted from PDF against rows from Power BI API.

## Project structure

- `reconciliation.py` - core business logic (mapping, comparison, summary, orchestration).
- `api_reader.py` - Power BI API authentication and query execution.
- `pdf_reader.py` - OCR extraction of `inc(a)` and `MAWB` from PDF.
- `cli.py` - command-line entry point.
- `streamlit_app.py` - web UI for upload and reconciliation.

## Streamlit app

Install dependencies:

```bash
pip install -r requirements.txt
```

Run app:

```bash
streamlit run streamlit_app.py
```

Flow in app:

- Upload PDF file.
- App extracts MAWB and amount from PDF.
- App fetches data from Power BI API.
- App compares PDF vs API and shows summary, mismatches, and full table.

## Run

```bash
python cli.py
```

Optional arguments:

- `--pdf /path/to/file.pdf`
- `--table taksimpol_checking`
- `--scale 2.0 --rotation 270 --psm 11 --auto-rotate`

## Power BI API settings

The script uses environment variables (defaults are already set in code):

- `PBI_CLIENT_ID`
- `PBI_USERNAME`
- `PBI_PASSWORD`
- `PBI_DATASET_ID`
- `PBI_TABLE`

Example:

```bash
export PBI_PASSWORD='your_password'
python cli.py
```
