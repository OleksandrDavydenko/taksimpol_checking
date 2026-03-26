from __future__ import annotations

import argparse
import os
import re
import sys

import pandas as pd
import requests

CLIENT_ID = os.getenv("PBI_CLIENT_ID", "706d72b2-a9a2-4d90-b0d8-b08f58459ef6")
USERNAME = os.getenv("PBI_USERNAME", "")
PASSWORD = os.getenv("PBI_PASSWORD", "")
DATASET_ID = os.getenv("PBI_DATASET_ID", "8b80be15-7b31-49e4-bc85-8b37a0d98f1c")
TABLE_NAME = os.getenv("PBI_TABLE", "taksimpol_checking")

TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/token"
PBI_SCOPE = "https://analysis.windows.net/powerbi/api"


def normalize_column_name(name: str) -> str:
    return re.sub(r"\s+", "", str(name).lower())


def get_token() -> str:
    if not USERNAME:
        raise RuntimeError("PBI_USERNAME is empty. Set it in environment variables.")
    if not PASSWORD:
        raise RuntimeError("PBI_PASSWORD is empty. Set it in environment variables.")

    body = {
        "grant_type": "password",
        "resource": PBI_SCOPE,
        "client_id": CLIENT_ID,
        "username": USERNAME,
        "password": PASSWORD,
    }
    response = requests.post(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    response.raise_for_status()
    token = response.json().get("access_token")
    if not token:
        raise RuntimeError("Access token not found in auth response.")
    return token


def execute_dax(token: str, dax: str) -> dict:
    url = f"https://api.powerbi.com/v1.0/myorg/datasets/{DATASET_ID}/executeQueries"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "queries": [{"query": dax}],
        "serializerSettings": {"includeNulls": True},
    }

    response = requests.post(url, headers=headers, json=payload, timeout=60)
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        print("Power BI query failed.", file=sys.stderr)
        print("DAX:", file=sys.stderr)
        print(dax, file=sys.stderr)
        print("Response:", file=sys.stderr)
        print(response.text, file=sys.stderr)
        raise exc

    return response.json()


def build_dax(table_name: str) -> str:
    return f"""
EVALUATE
SELECTCOLUMNS(
    '{table_name}',
    "MAWB", '{table_name}'[MAWB],
    "SUM_USD", '{table_name}'[SUM_USD],
    "ugoda", '{table_name}'[ugoda],
    "PaymentTypeFromInformInvoice", '{table_name}'[PaymentTypeFromInformInvoice]
)
"""


def _to_dataframe(result_json: dict) -> pd.DataFrame:
    results = result_json.get("results", [])
    tables = results[0].get("tables", []) if results else []
    if not tables:
        return pd.DataFrame()

    table = tables[0]
    columns = [c.get("name") for c in table.get("columns", [])] if table.get("columns") else []
    rows = table.get("rows", []) or []

    records = []
    for row in rows:
        if isinstance(row, dict):
            records.append(row)
        else:
            records.append({columns[i]: row[i] for i in range(len(columns))})

    def clean_column(name: str) -> str:
        if not isinstance(name, str):
            return str(name)
        if "[" in name and name.endswith("]"):
            return name.split("[", 1)[1][:-1]
        return name

    cleaned = [{clean_column(k): v for k, v in item.items()} for item in records]
    return pd.DataFrame(cleaned)


def read_powerbi_table(table_name: str = TABLE_NAME) -> pd.DataFrame:
    token = get_token()
    dax = build_dax(table_name)
    result = execute_dax(token, dax)
    return _to_dataframe(result)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read rows from Power BI table and print as DataFrame."
    )
    parser.add_argument(
        "--table",
        default=TABLE_NAME,
        help="Power BI table name (default from PBI_TABLE env).",
    )
    args = parser.parse_args()

    df = read_powerbi_table(args.table)
    if df.empty:
        print("No rows returned.")
        return 0

    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 200)
    print(f"Reading table: {args.table}")
    print(f"Returned rows: {len(df)}")
    try:
        print(df.to_string(index=False))
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())