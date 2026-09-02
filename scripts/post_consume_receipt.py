#!/usr/bin/env python3
"""paperless-ngx post-consume hook: extract receipt info via Ollama and
append a row to the current period's Connx-format expense-statement CSV,
saving a copy of the source receipt file alongside it.

Configured entirely via env vars -- stdlib only, no external dependencies,
since post-consume scripts run inside whatever Python environment the
paperless container already has, not a dedicated venv for this script.
"""

import csv, fcntl, io, json, os, re, sys, urllib.error, urllib.parse, urllib.request, uuid
from datetime import date, datetime, timedelta
from pathlib import Path

TEMPLATE_HEADER = """\
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
"""
TEMPLATE_LINE_ITEM_ROW = ",,,,,,,,,,$0.00,\n"
TEMPLATE_LINE_ITEM_ROW_COUNT = 20
TEMPLATE_TOTALS = """\
,,,0.00,0.00,0.00,0.00,0.00,0.00,0.00,,
,,,,,,,,Subtotal,,0.00,
,,,,,,,,Advances,,,
,,,,,,,,TOTAL,,0.00,
Approved,,Notes,,,,,,,,,
"""
TEMPLATE_BLANK_ROW = ",,,,,,,,,,,\n"
TEMPLATE_BLANK_ROW_COUNT = 6
TEMPLATE_FOOTER = """\
For Office Use Only,,,,,,,,,,,
,,,,,,,,,,,
"""
TEMPLATE_CSV = TEMPLATE_HEADER + TEMPLATE_LINE_ITEM_ROW * TEMPLATE_LINE_ITEM_ROW_COUNT + TEMPLATE_TOTALS + TEMPLATE_BLANK_ROW * TEMPLATE_BLANK_ROW_COUNT + TEMPLATE_FOOTER

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

# Original filename prefix used for CSVs this script itself uploads back into
# paperless (see sync_csv_to_paperless). Consuming one of those must never be
# treated as a new receipt -- run() bails out on this before anything else.
EXPENSE_CSV_FILENAME_PREFIX = "Expense_Statement_"


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
        raise RuntimeError(f"Document {document_id} has no OCR content yet.")
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
        raise RuntimeError(f"No valid amount extracted (got {amount!r}) -- refusing to guess, skipping.") from err

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
        next_month = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
        return start, next_month - timedelta(days=1)

    if cadence == "semimonthly":
        if receipt_date.day <= 15:
            return receipt_date.replace(day=1), receipt_date.replace(day=15)
        start = receipt_date.replace(day=16)
        next_month = start.replace(year=start.year + 1, month=1, day=1) if start.month == 12 else start.replace(month=start.month + 1, day=1)
        return start, next_month - timedelta(days=1)

    if cadence == "biweekly":
        if anchor_date is None:
            raise SystemExit("EXPENSE_PERIOD_ANCHOR is required when EXPENSE_PERIOD_CADENCE=biweekly (no calendar-natural biweekly boundary without one).")
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
    header_index = next(i for i, row in enumerate(rows) if len(row) > 1 and row[0] == "Date" and row[1] == "Description")
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


def _delete_existing_csv_documents(title, paperless_url, headers):
    search_url = f"{paperless_url.rstrip('/')}/api/documents/?title__iexact={urllib.parse.quote(title)}"
    request = urllib.request.Request(search_url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        existing = json.loads(response.read().decode("utf-8")).get("results", [])
    for doc in existing:
        delete_request = urllib.request.Request(
            f"{paperless_url.rstrip('/')}/api/documents/{doc['id']}/",
            headers=headers,
            method="DELETE",
        )
        urllib.request.urlopen(delete_request, timeout=30).close()


def _multipart_body(boundary, title, filename, content_type, file_bytes):
    parts = io.BytesIO()
    parts.write(f"--{boundary}\r\n".encode("utf-8"))
    parts.write(b'Content-Disposition: form-data; name="title"\r\n\r\n')
    parts.write(title.encode("utf-8"))
    parts.write(b"\r\n")
    parts.write(f"--{boundary}\r\n".encode("utf-8"))
    parts.write(f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'.encode("utf-8"))
    parts.write(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
    parts.write(file_bytes)
    parts.write(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return parts.getvalue()


# --- Minimal PDF rendering ---------------------------------------------
#
# gotenberg's LibreOffice conversion (already in this stack) renders the
# CSV's full 12-column Connx layout by splitting it across several pages
# with truncated columns -- not remotely readable. Since the receipt data
# is fully known at this point anyway, render a focused summary (date,
# vendor, category, amount, grand total) as a PDF by hand instead. Plain
# text-drawing/line PDF operators only, no embedded fonts -- small enough
# not to be worth a dependency the container may not have.

PDF_PAGE_WIDTH = 612
PDF_PAGE_HEIGHT = 792
PDF_MARGIN = 50
PDF_ROW_HEIGHT = 16
PDF_ROWS_PER_PAGE = 30
PDF_COLUMNS = [("Date", 70), ("Description", 260), ("Category", 80), ("Amount", 72)]


def _pdf_escape(text):
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _pdf_text(x, y, size, text, font="F1"):
    return f"BT /{font} {size} Tf 1 0 0 1 {x:.2f} {y:.2f} Tm ({_pdf_escape(text)}) Tj ET\n"


def _pdf_hline(x1, x2, y):
    return f"{x1:.2f} {y:.2f} m {x2:.2f} {y:.2f} l S\n"


def _extract_statement_summary(rows):
    name_row_index = next(i for i, row in enumerate(rows) if row and row[0] == "Name")
    to_row_index = name_row_index + 1
    employee_name = rows[name_row_index][1]
    period_start = rows[name_row_index][10]
    period_end = rows[to_row_index][10]

    header_index, subtotal_row_index = _find_table_bounds(rows)
    items = []
    for i in range(header_index + 1, subtotal_row_index):
        row = rows[i]
        if not row[DATE_COL].strip():
            continue
        category = next((cat for cat, col in CATEGORY_COLUMNS.items() if row[col].strip()), "")
        items.append((row[DATE_COL], row[DESCRIPTION_COL][:45], category, row[TOTAL_COL]))

    total_row = next(i for i in range(subtotal_row_index + 1, len(rows)) if rows[i][8] == "TOTAL")
    grand_total = rows[total_row][TOTAL_COL]
    return employee_name, period_start, period_end, items, grand_total


def _pdf_table_page_content(title_lines, items_slice, grand_total_line, page_num, page_count):
    table_width = sum(width for _, width in PDF_COLUMNS)
    ops = []
    y = PDF_PAGE_HEIGHT - PDF_MARGIN
    for text, size, font in title_lines:
        ops.append(_pdf_text(PDF_MARGIN, y, size, text, font))
        y -= size + 6
    y -= 10

    x = PDF_MARGIN
    for label, width in PDF_COLUMNS:
        ops.append(_pdf_text(x + 2, y, 9, label, "F2"))
        x += width
    y -= 4
    ops.append(_pdf_hline(PDF_MARGIN, PDF_MARGIN + table_width, y))
    y -= PDF_ROW_HEIGHT

    for item_date, description, category, amount in items_slice:
        x = PDF_MARGIN
        for value, (_, width) in zip((item_date, description, category, amount), PDF_COLUMNS, strict=True):
            ops.append(_pdf_text(x + 2, y, 9, value, "F1"))
            x += width
        y -= PDF_ROW_HEIGHT

    if grand_total_line is not None:
        y -= 6
        ops.append(_pdf_hline(PDF_MARGIN, PDF_MARGIN + table_width, y + PDF_ROW_HEIGHT - 4))
        label_x = PDF_MARGIN + table_width - PDF_COLUMNS[-1][1] - 80
        ops.append(_pdf_text(label_x, y, 10, "Grand Total:", "F2"))
        ops.append(_pdf_text(PDF_MARGIN + table_width - PDF_COLUMNS[-1][1] + 2, y, 10, grand_total_line, "F2"))

    ops.append(_pdf_text(PDF_PAGE_WIDTH - PDF_MARGIN - 70, PDF_MARGIN - 25, 8, f"Page {page_num} of {page_count}"))
    return "".join(ops).encode("utf-8")


def _pdf_object_bytes(num, body):
    return f"{num} 0 obj\n".encode("latin-1") + body + b"\nendobj\n"


def _pdf_stream_object(content_bytes):
    return f"<< /Length {len(content_bytes)} >>\nstream\n".encode("latin-1") + content_bytes + b"\nendstream"


def _build_pdf(pages_content):
    font_f1_num, font_f2_num = 3, 4
    first_page_num = 5
    page_nums = list(range(first_page_num, first_page_num + len(pages_content)))
    content_nums = list(range(first_page_num + len(pages_content), first_page_num + 2 * len(pages_content)))

    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{' '.join(f'{p} 0 R' for p in page_nums)}] /Count {len(page_nums)} >>".encode(
            "latin-1"
        ),
        font_f1_num: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        font_f2_num: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
    }
    for page_num, content_num in zip(page_nums, content_nums, strict=True):
        objects[page_num] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PDF_PAGE_WIDTH} {PDF_PAGE_HEIGHT}] "
            f"/Resources << /Font << /F1 {font_f1_num} 0 R /F2 {font_f2_num} 0 R >> >> "
            f"/Contents {content_num} 0 R >>"
        ).encode("latin-1")
    for content_num, content_bytes in zip(content_nums, pages_content, strict=True):
        objects[content_num] = _pdf_stream_object(content_bytes)

    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += _pdf_object_bytes(num, objects[num])
    xref_offset = len(out)
    count = max(objects) + 1
    out += f"xref\n0 {count}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for num in range(1, count):
        out += f"{offsets.get(num, 0):010d} 00000 n \n".encode("latin-1")
    out += b"trailer\n" + f"<< /Size {count} /Root 1 0 R >>\n".encode("latin-1")
    out += f"startxref\n{xref_offset}\n%%EOF".encode("latin-1")
    return bytes(out)


def render_statement_pdf(rows):
    employee_name, period_start, period_end, items, grand_total = _extract_statement_summary(rows)
    title_lines = [
        ("Expense Statement", 16, "F2"),
        (f"Employee: {employee_name}", 10, "F1"),
        (f"Period: {period_start} to {period_end}", 10, "F1"),
    ]
    pages_items = [items[i : i + PDF_ROWS_PER_PAGE] for i in range(0, len(items), PDF_ROWS_PER_PAGE)] or [[]]
    page_count = len(pages_items)
    pages_content = [
        _pdf_table_page_content(
            title_lines if page_num == 1 else [],
            page_items,
            grand_total if page_num == page_count else None,
            page_num,
            page_count,
        )
        for page_num, page_items in enumerate(pages_items, start=1)
    ]
    return _build_pdf(pages_content)


def sync_csv_to_paperless(csv_path, title, paperless_url, api_token):
    """Make the current expense statement visible in the paperless UI as a
    readable PDF summary: replace any previous upload with the same title
    (each receipt appended changes the file, and documents can't be updated
    in place) with a fresh one reflecting the latest state. The CSV on disk
    -- the actual Connx-template deliverable -- is untouched by this."""
    headers = {"Authorization": f"Token {api_token}"}
    _delete_existing_csv_documents(title, paperless_url, headers)

    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    pdf_bytes = render_statement_pdf(rows)

    boundary = uuid.uuid4().hex
    upload_request = urllib.request.Request(
        f"{paperless_url.rstrip('/')}/api/documents/post_document/",
        data=_multipart_body(boundary, title, f"{csv_path.stem}.pdf", "application/pdf", pdf_bytes),
        headers={**headers, "Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    urllib.request.urlopen(upload_request, timeout=60).close()


def run():
    original_filename = _env("DOCUMENT_ORIGINAL_FILENAME", "")
    if original_filename.startswith(EXPENSE_CSV_FILENAME_PREFIX):
        # One of our own CSV uploads getting consumed again -- never treat
        # it as a new receipt, regardless of what tags matched on it.
        return

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
    output_dir = Path(_env("EXPENSE_OUTPUT_DIR", "/usr/src/paperless/media/expense-statements"))
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        ocr_text = fetch_document_content(document_id, paperless_url, api_token)
        extracted = call_ollama_extract(ocr_text, ollama_base_url, ollama_model)
        entry = validate_extraction(extracted, fallback_date)
    except (RuntimeError, urllib.error.URLError, json.JSONDecodeError, KeyError) as err:
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
    csv_title = f"Expense Statement {period_name}"
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            append_receipt(csv_path, employee_name, period_start, period_end, entry)
            downloaded_path.rename(final_receipt_path)
            try:
                sync_csv_to_paperless(csv_path, csv_title, paperless_url, api_token)
            except urllib.error.URLError as err:
                # The CSV on disk is already correct; only the UI-visible
                # copy in paperless failed to refresh. Don't fail the whole
                # run over that.
                print(f"WARNING: failed to sync {csv_path} into paperless: {err}", file=sys.stderr)
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
