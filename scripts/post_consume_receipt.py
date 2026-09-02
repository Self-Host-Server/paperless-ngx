#!/usr/bin/env python3
"""paperless-ngx post-consume hook: extract receipt info via Ollama and
append a row to the current period's Connx-format expense-statement CSV,
saving a copy of the source receipt file alongside it.

Configured entirely via env vars -- stdlib only, no external dependencies,
since post-consume scripts run inside whatever Python environment the
paperless container already has, not a dedicated venv for this script.
"""
import csv
import fcntl
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

TEMPLATE_CSV = """\
,,,,,,,Statement Number:,,,,
,,,Connx INC,,,,,,,,
Expense Statement,,,"103 Morgan Lane,Plainsboro, NJ 08536. Ph: 609-955-3030",,,,,,,,
,,,,,,,,,,,
Purpose:,,,,,,,,,,,
,,,,,,,,,,,
Employee Information,,,,,,,,,Pay Period,,
Name,,,Department,,,,,,From,,
SSN,,,Position,,,,,,To,,
Employee ID,,,Manager,,,,,,,,
,,,,,,,,,,,
Date,Description,,Hotel,Transport,Fuel,Meals,Phone,Entertain.,Misc.,TOTAL,
,,,,,,,,,,$0.00,
,,,,,,,,,,$0.00,
,,,,,,,,,,$0.00,
,,,,,,,,,,$0.00,
,,,,,,,,,,$0.00,
,,,,,,,,,,$0.00,
,,,,,,,,,,$0.00,
,,,,,,,,,,$0.00,
,,,,,,,,,,$0.00,
,,,,,,,,,,$0.00,
,,,,,,,,,,$0.00,
,,,,,,,,,,$0.00,
,,,,,,,,,,$0.00,
,,,,,,,,,,$0.00,
,,,,,,,,,,$0.00,
,,,,,,,,,,$0.00,
,,,,,,,,,,$0.00,
,,,,,,,,,,$0.00,
,,,,,,,,,,$0.00,
,,,,,,,,,,$0.00,
,,,0.00,0.00,0.00,0.00,0.00,0.00,0.00,,
,,,,,,,,Subtotal,,0.00,
,,,,,,,,Advances,,,
,,,,,,,,TOTAL,,0.00,
Approved,,Notes,,,,,,,,,
,,,,,,,,,,,
,,,,,,,,,,,
,,,,,,,,,,,
,,,,,,,,,,,
,,,,,,,,,,,
,,,,,,,,,,,
For Office Use Only,,,,,,,,,,,
,,,,,,,,,,,
"""

CATEGORY_COLUMNS = {
    "Hotel": 3,
    "Transport": 4,
    "Fuel": 5,
    "Meals": 6,
    "Phone": 7,
    "Entertain.": 8,
    "Misc.": 9,
}
VALID_CATEGORIES = set(CATEGORY_COLUMNS)
DATE_COL = 0
DESCRIPTION_COL = 1
TOTAL_COL = 10


class ExtractionError(Exception):
    pass


def _env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        raise SystemExit(f"Missing required env var: {name}")
    return value


def fetch_document_content(document_id, paperless_url, api_token):
    request = urllib.request.Request(
        f"{paperless_url.rstrip('/')}/api/documents/{document_id}/",
        headers={"Authorization": f"Token {api_token}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
    content = data.get("content")
    if not content:
        raise ExtractionError(f"Document {document_id} has no OCR content yet.")
    return content


EXTRACTION_PROMPT_TEMPLATE = """You are extracting expense information from a receipt's OCR text for an expense report.

Return ONLY a JSON object with exactly these keys:
- "date": the receipt's date in YYYY-MM-DD format, taken directly from the text. If no date is clearly present, use null.
- "description": a short (under 60 characters) description of the vendor/purchase, taken from the text.
- "category": exactly one of "Hotel", "Transport", "Fuel", "Meals", "Phone", "Entertain.", "Misc." -- pick the single best fit. Use "Misc." if genuinely ambiguous or it doesn't clearly fit another category.
- "amount": the total amount as a plain number (no currency symbol, no commas), taken directly from the text. If no clear total amount is present, use null.

Only use values that are actually present in the text below -- never invent a date, description, or amount that isn't there. If a field is genuinely not determinable from the text, use null for that field rather than guessing.

Receipt text:
\"\"\"
{ocr_text}
\"\"\"
"""


def call_ollama_extract(ocr_text, ollama_base_url, model):
    prompt = EXTRACTION_PROMPT_TEMPLATE.format(ocr_text=ocr_text)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "format": "json",
        "stream": False,
    }
    request = urllib.request.Request(
        f"{ollama_base_url.rstrip('/')}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        result = json.loads(response.read().decode("utf-8"))
    return json.loads(result["message"]["content"])


def validate_extraction(extracted, fallback_date):
    category = extracted.get("category")
    if category not in VALID_CATEGORIES:
        print(f"WARNING: LLM returned invalid category {category!r}, defaulting to Misc.", file=sys.stderr)
        category = "Misc."

    amount = extracted.get("amount")
    try:
        amount = float(amount)
        if amount <= 0:
            raise ValueError
    except (TypeError, ValueError) as err:
        raise ExtractionError(f"No valid amount extracted (got {amount!r}) -- refusing to guess, skipping.") from err

    receipt_date = fallback_date
    date_str = extracted.get("date")
    if date_str:
        try:
            receipt_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            print(
                f"WARNING: could not parse extracted date {date_str!r}, falling back to DOCUMENT_CREATED",
                file=sys.stderr,
            )

    description = (extracted.get("description") or "").strip()[:60] or "Receipt"

    return {"date": receipt_date, "description": description, "category": category, "amount": amount}


def compute_period(cadence, receipt_date, anchor_date=None):
    if cadence == "monthly":
        start = receipt_date.replace(day=1)
        next_month = (
            start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
        )
        return start, next_month - timedelta(days=1)

    if cadence == "semimonthly":
        if receipt_date.day <= 15:
            return receipt_date.replace(day=1), receipt_date.replace(day=15)
        start = receipt_date.replace(day=16)
        next_month = (
            start.replace(year=start.year + 1, month=1, day=1)
            if start.month == 12
            else start.replace(month=start.month + 1, day=1)
        )
        return start, next_month - timedelta(days=1)

    if cadence == "biweekly":
        if anchor_date is None:
            raise SystemExit(
                "EXPENSE_PERIOD_ANCHOR is required when EXPENSE_PERIOD_CADENCE=biweekly "
                "(no calendar-natural biweekly boundary without one)."
            )
        cycle_index = (receipt_date - anchor_date).days // 14
        start = anchor_date + timedelta(days=cycle_index * 14)
        return start, start + timedelta(days=13)

    raise SystemExit(f"Unknown EXPENSE_PERIOD_CADENCE: {cadence!r}")


def _load_or_create_rows(csv_path, period_start, period_end, employee_name):
    if csv_path.exists():
        with open(csv_path, newline="", encoding="utf-8") as f:
            return list(csv.reader(f))

    rows = list(csv.reader(io.StringIO(TEMPLATE_CSV)))
    name_row_index = next(i for i, row in enumerate(rows) if row and row[0] == "Name")
    to_row_index = name_row_index + 1
    rows[name_row_index][1] = employee_name
    rows[name_row_index][10] = period_start.isoformat()
    rows[to_row_index][10] = period_end.isoformat()
    return rows


def _find_table_bounds(rows):
    header_index = next(
        i for i, row in enumerate(rows) if len(row) > 1 and row[0] == "Date" and row[1] == "Description"
    )
    subtotal_row_index = next(i for i in range(header_index + 1, len(rows)) if not rows[i][TOTAL_COL].strip())
    return header_index, subtotal_row_index


def _find_or_insert_target_row(rows, header_index, subtotal_row_index):
    for i in range(header_index + 1, subtotal_row_index):
        if not rows[i][DATE_COL].strip():
            return i, rows, subtotal_row_index

    blank_row = next(iter(csv.reader(io.StringIO(",,,,,,,,,,$0.00,\n"))))
    rows.insert(subtotal_row_index, blank_row)
    return subtotal_row_index, rows, subtotal_row_index + 1


def _recompute_totals(rows, header_index, subtotal_row_index):
    category_sums = {category: 0.0 for category in CATEGORY_COLUMNS}
    for i in range(header_index + 1, subtotal_row_index):
        for category, col in CATEGORY_COLUMNS.items():
            cell = rows[i][col].strip()
            if cell:
                category_sums[category] += float(cell.replace("$", "").replace(",", ""))

    for category, col in CATEGORY_COLUMNS.items():
        rows[subtotal_row_index][col] = f"{category_sums[category]:.2f}"

    subtotal = sum(category_sums.values())
    subtotal_label_row = next(i for i in range(subtotal_row_index + 1, len(rows)) if rows[i][8] == "Subtotal")
    advances_row = next(i for i in range(subtotal_row_index + 1, len(rows)) if rows[i][8] == "Advances")
    total_row = next(i for i in range(subtotal_row_index + 1, len(rows)) if rows[i][8] == "TOTAL")

    rows[subtotal_label_row][TOTAL_COL] = f"{subtotal:.2f}"

    advances_cell = rows[advances_row][TOTAL_COL].strip()
    advances = float(advances_cell.replace("$", "").replace(",", "")) if advances_cell else 0.0
    rows[total_row][TOTAL_COL] = f"{subtotal - advances:.2f}"


def append_receipt(csv_path, employee_name, period_start, period_end, entry):
    rows = _load_or_create_rows(csv_path, period_start, period_end, employee_name)
    header_index, subtotal_row_index = _find_table_bounds(rows)
    target_row_index, rows, subtotal_row_index = _find_or_insert_target_row(rows, header_index, subtotal_row_index)

    rows[target_row_index][DATE_COL] = entry["date"].isoformat() if entry["date"] else ""
    rows[target_row_index][DESCRIPTION_COL] = entry["description"]
    rows[target_row_index][CATEGORY_COLUMNS[entry["category"]]] = f"{entry['amount']:.2f}"
    rows[target_row_index][TOTAL_COL] = f"${entry['amount']:.2f}"

    _recompute_totals(rows, header_index, subtotal_row_index)

    temp_path = csv_path.with_suffix(".csv.tmp")
    with open(temp_path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    temp_path.replace(csv_path)


def _extension_from_headers(headers, fallback_filename):
    content_disposition = headers.get("Content-Disposition", "")
    match = re.search(r'filename="?([^";]+)"?', content_disposition)
    filename = match.group(1) if match else fallback_filename
    suffix = Path(filename).suffix if filename else ""
    return suffix or ".pdf"


def download_receipt_file(url, api_token, dest_path, fallback_filename):
    request = urllib.request.Request(url, headers={"Authorization": f"Token {api_token}"})
    with urllib.request.urlopen(request, timeout=60) as response:
        extension = _extension_from_headers(response.headers, fallback_filename)
        final_temp_path = dest_path.with_suffix(extension)
        with open(final_temp_path, "wb") as f:
            f.write(response.read())
    return final_temp_path


def run():
    tags = _env("DOCUMENT_TAGS", "")
    receipt_tag = _env("PAPERLESS_RECEIPT_TAG", "Receipt")
    if receipt_tag not in [t.strip() for t in tags.split(",")]:
        return

    document_id = _env("DOCUMENT_ID", required=True)
    document_created = _env("DOCUMENT_CREATED", required=True)
    fallback_date = datetime.fromisoformat(document_created.replace("Z", "+00:00")).date()

    paperless_url = _env("PAPERLESS_URL", required=True)
    api_token = _env("PAPERLESS_API_TOKEN", required=True)
    ollama_base_url = _env("OLLAMA_BASE_URL", "http://192.168.1.14:11434")
    ollama_model = _env("OLLAMA_EXTRACTION_MODEL", "qwen2.5:7b")
    cadence = _env("EXPENSE_PERIOD_CADENCE", "monthly")
    anchor_str = _env("EXPENSE_PERIOD_ANCHOR")
    anchor_date = date.fromisoformat(anchor_str) if anchor_str else None
    employee_name = _env("EMPLOYEE_NAME", required=True)
    output_dir = Path(_env("EXPENSE_OUTPUT_DIR", "/data/expense-statements"))
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        ocr_text = fetch_document_content(document_id, paperless_url, api_token)
        extracted = call_ollama_extract(ocr_text, ollama_base_url, ollama_model)
        entry = validate_extraction(extracted, fallback_date)
    except (ExtractionError, urllib.error.URLError, json.JSONDecodeError, KeyError) as err:
        print(f"ERROR: receipt automation failed for document {document_id}: {err}", file=sys.stderr)
        return

    period_start, period_end = compute_period(cadence, entry["date"] or fallback_date, anchor_date)
    period_name = f"{period_start.isoformat()}_{period_end.isoformat()}"
    period_dir = output_dir / period_name
    receipts_dir = period_dir / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    csv_path = period_dir / f"Expense_Statement_{period_name}.csv"

    document_filename = _env("DOCUMENT_FILE_NAME")
    # DOCUMENT_DOWNLOAD_URL (set by paperless itself) is a relative path with
    # no query string -- it isn't directly usable as a request URL, and it
    # would serve the archive/OCR'd derivative rather than the original
    # receipt image. Always build our own absolute URL with ?original=true
    # instead of trusting that env var.
    document_download_url = f"{paperless_url.rstrip('/')}/api/documents/{document_id}/download/?original=true"
    row_date = entry["date"].isoformat() if entry["date"] else fallback_date.isoformat()
    temp_receipt_path = receipts_dir / f".tmp-{document_id}"

    try:
        downloaded_path = download_receipt_file(document_download_url, api_token, temp_receipt_path, document_filename)
    except urllib.error.URLError as err:
        print(f"ERROR: failed to download receipt file for document {document_id}: {err}", file=sys.stderr)
        return

    lock_path = output_dir / ".expense-statement.lock"
    final_receipt_path = receipts_dir / f"{row_date}_{document_id}{downloaded_path.suffix}"
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            append_receipt(csv_path, employee_name, period_start, period_end, entry)
            downloaded_path.rename(final_receipt_path)
        except Exception:
            downloaded_path.unlink(missing_ok=True)
            raise
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)

    print(f"Added receipt from document {document_id} to {csv_path} (file: {final_receipt_path})")


if __name__ == "__main__":
    try:
        run()
    except Exception as err:
        print(f"ERROR: unhandled failure in post-consume receipt hook: {err}", file=sys.stderr)
