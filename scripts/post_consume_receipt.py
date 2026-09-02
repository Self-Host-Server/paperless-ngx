#!/usr/bin/env python3
"""paperless-ngx post-consume hook: extract receipt info via Ollama and
append a row to a Connx-format expense-statement CSV, saving a copy of
the source receipt file alongside it.

Receipts are grouped by a tag (e.g. a trip or project name), not by
calendar period: a document tagged both PAPERLESS_RECEIPT_TAG and
exactly one other tag joins that other tag's statement. A receipt with
no such group tag is left alone entirely. Tagging a (any) document with
both PAPERLESS_CLEAR_TAG and a group tag wipes that group's statement
instead of processing it as a receipt -- see clear_statement().

Configured entirely via env vars -- stdlib only, no external dependencies,
since post-consume scripts run inside whatever Python environment the
paperless container already has, not a dedicated venv for this script.
"""

import csv, fcntl, io, json, os, re, shutil, sys, urllib.error, urllib.parse, urllib.request, uuid
from datetime import datetime
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


def fetch_employee_name(owner_username, paperless_url, api_token):
    """The statement's "Name" field comes from whichever paperless account
    owns the receipt, not a fixed config value -- so it's correct even when
    several people share one instance. Prefers the account's real name,
    falling back to the username if that isn't set."""
    request = urllib.request.Request(
        f"{paperless_url.rstrip('/')}/api/users/?username={urllib.parse.quote(owner_username)}",
        headers={"Authorization": f"Token {api_token}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        results = json.loads(response.read().decode("utf-8")).get("results", [])
    if not results:
        return owner_username
    user = results[0]
    full_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
    return full_name or owner_username


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


def _slugify(text):
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")
    return slug or "untitled"


def _resolve_group_tag(tags, excluded_tags):
    """The group a receipt (or a clear-statement trigger) belongs to is
    whichever tag is left after excluding the fixed control tags. Zero or
    more than one candidate is treated as "no group" -- silently guessing
    between several tags would risk filing a receipt (or clearing a
    statement) under the wrong one."""
    candidates = [t for t in tags if t not in excluded_tags]
    return candidates[0] if len(candidates) == 1 else None


def _load_or_create_rows(csv_path, group_tag):
    if csv_path.exists():
        with open(csv_path, newline="", encoding="utf-8") as f:
            return list(csv.reader(f))

    rows = list(csv.reader(io.StringIO(TEMPLATE_CSV)))
    purpose_row_index = next(i for i, row in enumerate(rows) if row and row[0] == "Purpose:")
    rows[purpose_row_index][1] = group_tag
    return rows


def _update_employee_name(rows, employee_name):
    """Unlike Purpose (the group tag, fixed at creation), Name reflects
    whoever owns the most recently appended receipt -- several people can
    share a group tag, and the template only has room for one name."""
    name_row_index = next(i for i, row in enumerate(rows) if row and row[0] == "Name")
    rows[name_row_index][1] = employee_name


def _update_date_range(rows, header_index, subtotal_row_index):
    """Pay Period From/To no longer reflect a fixed calendar period (there
    isn't one -- grouping is by tag) -- keep them as the actual date range
    the group's receipts span instead."""
    dates = sorted(rows[i][DATE_COL] for i in range(header_index + 1, subtotal_row_index) if rows[i][DATE_COL].strip())
    if not dates:
        return
    name_row_index = next(i for i, row in enumerate(rows) if row and row[0] == "Name")
    rows[name_row_index][10] = dates[0]
    rows[name_row_index + 1][10] = dates[-1]


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


def append_receipt(csv_path, employee_name, group_tag, entry):
    rows = _load_or_create_rows(csv_path, group_tag)
    header_index, subtotal_row_index = _find_table_bounds(rows)
    target_row_index, rows, subtotal_row_index = _find_or_insert_target_row(rows, header_index, subtotal_row_index)

    rows[target_row_index][DATE_COL] = entry["date"].isoformat() if entry["date"] else ""
    rows[target_row_index][DESCRIPTION_COL] = entry["description"]
    rows[target_row_index][CATEGORY_COLUMNS[entry["category"]]] = f"{entry['amount']:.2f}"
    rows[target_row_index][TOTAL_COL] = f"${entry['amount']:.2f}"

    _update_employee_name(rows, employee_name)
    _update_date_range(rows, header_index, subtotal_row_index)
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


def _find_documents_by_title(title, paperless_url, headers):
    search_url = f"{paperless_url.rstrip('/')}/api/documents/?title__iexact={urllib.parse.quote(title)}"
    request = urllib.request.Request(search_url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8")).get("results", [])


def _delete_existing_csv_documents(title, paperless_url, headers):
    for doc in _find_documents_by_title(title, paperless_url, headers):
        delete_request = urllib.request.Request(
            f"{paperless_url.rstrip('/')}/api/documents/{doc['id']}/",
            headers=headers,
            method="DELETE",
        )
        urllib.request.urlopen(delete_request, timeout=30).close()


# sync_csv_to_paperless deliberately does NOT wait for its post_document
# upload to finish consuming before returning. paperless's default deploy
# runs its consumer at concurrency=1, and the post-consume script executes
# synchronously inside that same worker's task -- so polling here for the
# upload's own consume task to complete would deadlock: that task can only
# run on the one worker slot this script is already occupying. Fire-and-
# forget instead, and rely on _delete_existing_csv_documents (which removes
# every matching-titled document, not just one) to self-heal on the next
# receipt if two uploads for the same group are ever in flight at once --
# a narrow window that in practice only opens for near-simultaneous
# uploads to the same group, not the one-receipt-at-a-time pattern this
# hook is built for.


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


def _statement_title(group_tag):
    return f"Expense Statement - {group_tag}"


def sync_csv_to_paperless(csv_path, group_tag, paperless_url, api_token):
    """Make the current expense statement CSV visible in the paperless UI:
    replace any previous upload for this group (each receipt appended
    changes the file, and documents can't be updated in place) with the
    current one."""
    title = _statement_title(group_tag)
    headers = {"Authorization": f"Token {api_token}"}
    _delete_existing_csv_documents(title, paperless_url, headers)

    boundary = uuid.uuid4().hex
    upload_request = urllib.request.Request(
        f"{paperless_url.rstrip('/')}/api/documents/post_document/",
        data=_multipart_body(boundary, title, csv_path.name, "text/csv", csv_path.read_bytes()),
        headers={**headers, "Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    urllib.request.urlopen(upload_request, timeout=60).close()


def clear_statement(group_tag, output_dir, paperless_url, api_token):
    """Wipe a group's CSV, receipts folder, and its synced paperless copy, so
    a group tag can be reused for a new batch without mixing in old data."""
    group_dir = output_dir / _slugify(group_tag)
    if group_dir.exists():
        shutil.rmtree(group_dir)
    headers = {"Authorization": f"Token {api_token}"}
    _delete_existing_csv_documents(_statement_title(group_tag), paperless_url, headers)


def run():
    original_filename = _env("DOCUMENT_ORIGINAL_FILENAME", "")
    if original_filename.startswith(EXPENSE_CSV_FILENAME_PREFIX):
        # One of our own CSV uploads getting consumed again -- never treat
        # it as a new receipt or a clear trigger, regardless of tags.
        return

    document_id = _env("DOCUMENT_ID", required=True)
    tags = [t.strip() for t in _env("DOCUMENT_TAGS", "").split(",") if t.strip()]
    receipt_tag = _env("PAPERLESS_RECEIPT_TAG", "Receipt")
    clear_tag = _env("PAPERLESS_CLEAR_TAG", "Clear Statement")
    control_tags = {receipt_tag, clear_tag}
    paperless_url = _env("PAPERLESS_URL", required=True)
    api_token = _env("PAPERLESS_API_TOKEN", required=True)
    output_dir = Path(_env("EXPENSE_OUTPUT_DIR", "/usr/src/paperless/media/expense-statements"))

    if clear_tag in tags:
        group_tag = _resolve_group_tag(tags, control_tags)
        if group_tag is None:
            print(
                f"ERROR: document {document_id} has {clear_tag!r} but no single group tag among {tags!r} "
                "-- refusing to guess which statement to clear.",
                file=sys.stderr,
            )
            return
        clear_statement(group_tag, output_dir, paperless_url, api_token)
        print(f"Cleared expense statement for group {group_tag!r} (triggered by document {document_id})")
        return

    if receipt_tag not in tags:
        return

    group_tag = _resolve_group_tag(tags, control_tags)
    if group_tag is None:
        print(
            f"Skipping document {document_id}: no single group tag among {tags!r} (besides {receipt_tag!r}) "
            "-- not added to any expense statement.",
            file=sys.stderr,
        )
        return

    owner_username = _env("DOCUMENT_OWNER", "")
    if not owner_username:
        print(
            f"Skipping document {document_id}: no owner set -- can't attribute this receipt to anyone.",
            file=sys.stderr,
        )
        return
    employee_name = fetch_employee_name(owner_username, paperless_url, api_token)

    document_created = _env("DOCUMENT_CREATED", required=True)
    fallback_date = datetime.fromisoformat(document_created.replace("Z", "+00:00")).date()
    ollama_base_url = _env("OLLAMA_BASE_URL", "http://192.168.1.14:11434")
    ollama_model = _env("OLLAMA_EXTRACTION_MODEL", "qwen2.5:7b")
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        ocr_text = fetch_document_content(document_id, paperless_url, api_token)
        extracted = call_ollama_extract(ocr_text, ollama_base_url, ollama_model)
        entry = validate_extraction(extracted, fallback_date)
    except (RuntimeError, urllib.error.URLError, json.JSONDecodeError, KeyError) as err:
        print(f"ERROR: receipt automation failed for document {document_id}: {err}", file=sys.stderr)
        return

    group_slug = _slugify(group_tag)
    group_dir = output_dir / group_slug
    receipts_dir = group_dir / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    csv_path = group_dir / f"Expense_Statement_{group_slug}.csv"

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

    lock_path = group_dir / ".expense-statement.lock"
    final_receipt_path = receipts_dir / f"{row_date}_{document_id}{downloaded_path.suffix}"
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            append_receipt(csv_path, employee_name, group_tag, entry)
            downloaded_path.rename(final_receipt_path)
            try:
                sync_csv_to_paperless(csv_path, group_tag, paperless_url, api_token)
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
