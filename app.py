"""
EMMY Cloths POS — Flask backend
Persists all data to a Google Sheet ("EMMY_Cloths_POS_Database") via gspread,
using a Google Service Account for authentication.

Run:
    pip install -r requirements.txt
    python app.py
"""

import os
import hmac
import secrets
from pathlib import Path
import re
import random
import threading
import time
import uuid
from datetime import datetime, timedelta

from flask import Flask, Response, jsonify, redirect, render_template_string, request, session, url_for
from werkzeug.exceptions import HTTPException
import gspread
from google.oauth2.service_account import Credentials
from werkzeug.utils import secure_filename

app = Flask(__name__)
APP_ENV = os.environ.get("APP_ENV", "development").strip().lower()
APP_HOST = os.environ.get("APP_HOST", "127.0.0.1")
APP_USERNAME = os.environ.get("APP_USERNAME", "owner")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
if APP_ENV == "production" and (len(APP_PASSWORD) < 16 or len(os.environ.get("SECRET_KEY", "")) < 32):
    raise RuntimeError("Production requires APP_PASSWORD (16+ characters) and SECRET_KEY (32+ characters).")
if APP_ENV != "production" and APP_HOST not in {"127.0.0.1", "localhost", "::1"}:
    raise RuntimeError("Public network binding requires APP_ENV=production and a protected login.")
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    MAX_CONTENT_LENGTH=5 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=(APP_ENV == "production"),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)

# --------------------------------------------------------------------------
# Google Sheets connection
# --------------------------------------------------------------------------
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
SPREADSHEET_NAME = os.environ.get("EMMY_SPREADSHEET_NAME", "EMMY_Cloths_POS_Database")

TAB_FABRIC = "Fabric_Inventory"
TAB_PRODUCTION = "Production_Log"
TAB_SALES = "Sales_Transactions"
TAB_EXPENSES = "Expenses_Salaries"
TAB_PRODUCTS = "Products"

HEADERS = {
    TAB_FABRIC: ["Fabric_ID", "Fabric_Type", "Roll_Meters", "Cost_Per_Meter", "Total_Cost", "Supplier", "Date"],
    TAB_PRODUCTION: ["Production_ID", "Fabric_ID", "Design_Name", "Size", "Qty_Produced", "Meters_Used", "Cost_Per_Piece", "Date"],
    TAB_SALES: ["Invoice_No", "Date_Time", "SKU_Barcode", "Item_Name", "Qty", "Unit_Price", "Total", "Payment_Method"],
    TAB_EXPENSES: ["Expense_ID", "Category", "Amount", "Paid_To", "Notes", "Date"],
    TAB_PRODUCTS: ["SKU", "Item_Name", "Size", "Unit_Price", "Stock", "Active", "Image_URL"],
}

DEFAULT_PRODUCTS = [
    ["EMMY-TS-BLK-M", "Cotton Crew Neck T-Shirt", "M", 2500, 40, "TRUE"],
    ["EMMY-TS-WHT-L", "Cotton Crew Neck T-Shirt", "L", 2500, 8, "TRUE"],
    ["EMMY-PL-NVY-S", "Classic Polo Shirt", "S", 3200, 25, "TRUE"],
    ["EMMY-DN-BLU-32", "Slim Fit Denim Jeans", "32", 6500, 15, "TRUE"],
    ["EMMY-HD-GRY-XL", "Fleece Hoodie", "XL", 5800, 5, "TRUE"],
    ["EMMY-SK-BLK-M", "A-Line Skirt", "M", 3400, 20, "TRUE"],
]

_client_cache = None
_spreadsheet_cache = None
_worksheet_cache = {}
_records_cache = {}
_cache_lock = threading.RLock()
_products_initialized = False
RECORD_CACHE_SECONDS = 10


def get_client():
    """Authenticate and cache a gspread client for the lifetime of the process."""
    global _client_cache
    if _client_cache is None:
        if not os.path.exists(SERVICE_ACCOUNT_FILE):
            raise FileNotFoundError(
                f"Service account key not found at '{SERVICE_ACCOUNT_FILE}'. "
                "See setup_instructions.md."
            )
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        _client_cache = gspread.authorize(creds)
    return _client_cache


def get_spreadsheet():
    global _spreadsheet_cache
    with _cache_lock:
        if _spreadsheet_cache is None:
            _spreadsheet_cache = get_client().open(SPREADSHEET_NAME)
        return _spreadsheet_cache


def sheets_read(operation):
    """Retry transient Google Sheets read failures with truncated exponential backoff."""
    for attempt in range(5):
        try:
            return operation()
        except gspread.exceptions.APIError as err:
            response = getattr(err, "response", None)
            status = getattr(response, "status_code", None)
            if (status != 429 and not (status is not None and 500 <= status < 600)) or attempt == 4:
                raise
            time.sleep(min(0.75 * (2 ** attempt) + random.random() * 0.5, 8))


def get_records(tab_name, force_refresh=False):
    """Return sheet records, cached briefly to avoid repeated quota-consuming reads."""
    with _cache_lock:
        cached = _records_cache.get(tab_name)
        if not force_refresh and cached and time.monotonic() - cached[0] < RECORD_CACHE_SECONDS:
            return cached[1]
        records = sheets_read(lambda: get_sheet(tab_name).get_all_records())
        _records_cache[tab_name] = (time.monotonic(), records)
        return records


def invalidate_records(*tab_names):
    with _cache_lock:
        for tab_name in tab_names:
            _records_cache.pop(tab_name, None)


def get_sheet(tab_name):
    """Return a cached worksheet, creating it with headers on first access."""
    with _cache_lock:
        if tab_name in _worksheet_cache:
            return _worksheet_cache[tab_name]
        sh = get_spreadsheet()
        try:
            ws = sh.worksheet(tab_name)
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=tab_name, rows=1000, cols=len(HEADERS[tab_name]))
            ws.append_row(HEADERS[tab_name], value_input_option="USER_ENTERED")
        else:
            if not sheets_read(ws.get_all_values):
                ws.append_row(HEADERS[tab_name], value_input_option="USER_ENTERED")
        _worksheet_cache[tab_name] = ws
        return ws


def get_products():
    global _products_initialized
    ws = get_sheet(TAB_PRODUCTS)
    with _cache_lock:
        if not _products_initialized:
            headers = sheets_read(lambda: ws.row_values(1))
            if "Image_URL" not in headers:
                if ws.col_count < len(HEADERS[TAB_PRODUCTS]):
                    ws.add_cols(len(HEADERS[TAB_PRODUCTS]) - ws.col_count)
                ws.update_cell(1, len(headers) + 1, "Image_URL")
            if not sheets_read(ws.get_all_records):
                ws.append_rows(DEFAULT_PRODUCTS, value_input_option="USER_ENTERED")
            _products_initialized = True
            invalidate_records(TAB_PRODUCTS)
    return ws


def next_id(prefix, ws):
    """Generate the next sequential ID from the cached records."""
    seq = len(get_records(ws.title)) + 1
    return f"{prefix}-{seq:04d}"


def to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign in | EMMY Cloths</title><style>*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f4f6fa;color:#172033;font:15px Inter,Segoe UI,Arial,sans-serif}.panel{width:min(420px,calc(100% - 32px));padding:34px;background:#fff;border:1px solid #e7ebf2;border-radius:22px;box-shadow:0 24px 70px #18233a12}.brand{font-size:23px;font-weight:800;color:#111c35}.brand span{color:#0e9f6e}.sub{color:#718096;margin:8px 0 26px}label{display:block;font-weight:650;font-size:13px;margin:15px 0 7px}input{width:100%;height:46px;padding:0 13px;border:1px solid #dce2ec;border-radius:10px;font:inherit}button{width:100%;height:46px;margin-top:22px;border:0;border-radius:10px;background:#0e9f6e;color:white;font:inherit;font-weight:700;cursor:pointer}.error{padding:11px;background:#fff0ef;color:#b42318;border-radius:9px;font-size:13px}</style></head><body><form class="panel" method="post"><div class="brand">EMMY <span>Cloths</span></div><p class="sub">Sign in to your store workspace.</p>{% if error %}<div class="error">{{ error }}</div>{% endif %}<input type="hidden" name="csrf_token" value="{{ csrf_token }}"><label for="username">Username</label><input id="username" name="username" autocomplete="username" required autofocus><label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" required><button type="submit">Sign in</button></form></body></html>"""


@app.before_request
def require_store_login():
    if not APP_PASSWORD or request.endpoint in {"login", "healthz"}:
        return None
    if not session.get("authenticated"):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Please sign in to continue.", "login_required": True}), 401
        return redirect(url_for("login", next=request.path))
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        supplied = request.headers.get("X-CSRF-Token", "")
        expected = session.get("csrf_token", "")
        if not expected or not hmac.compare_digest(supplied, expected):
            return jsonify({"error": "Your session expired. Refresh the page and try again."}), 400
    return None


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/login", methods=["GET", "POST"])
def login():
    csrf_token = session.setdefault("login_csrf", secrets.token_urlsafe(32))
    error = None
    if request.method == "POST":
        supplied_token = request.form.get("csrf_token", "")
        if not hmac.compare_digest(supplied_token, csrf_token):
            error = "Session expired. Refresh the page and try again."
        elif (hmac.compare_digest(request.form.get("username", ""), APP_USERNAME)
              and hmac.compare_digest(request.form.get("password", ""), APP_PASSWORD)):
            session.clear()
            session.permanent = True
            session["authenticated"] = True
            session["csrf_token"] = secrets.token_urlsafe(32)
            return redirect(url_for("index"))
        else:
            error = "Username or password is incorrect."
    return render_template_string(LOGIN_PAGE, error=error, csrf_token=csrf_token), 401 if error else 200


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    if APP_PASSWORD and not session.get("csrf_token"):
        session["csrf_token"] = secrets.token_urlsafe(32)
    html = Path(app.root_path, "index.html").read_text(encoding="utf-8")
    html = html.replace("__CSRF_TOKEN__", session.get("csrf_token", ""))
    html = html.replace("__LOGOUT_DISPLAY__", "inline-flex" if APP_PASSWORD else "none")
    return Response(html, mimetype="text/html")


# --------------------------------------------------------------------------
# API: Checkout
# --------------------------------------------------------------------------
@app.route("/api/checkout", methods=["POST"])
def checkout():
    data = request.get_json(force=True, silent=True) or {}
    items = data.get("items", [])
    payment_method = data.get("pay", "Cash")
    discount = max(0.0, to_float(data.get("disc", 0)))

    if not items:
        return jsonify({"error": "Cart is empty."}), 400

    invoice_no = f"EMMY-INV-{int(datetime.now().timestamp())}"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    ws = get_sheet(TAB_SALES)
    products_ws = get_products()
    products = get_records(TAB_PRODUCTS)
    product_records = {str(row.get("SKU", "")): row for row in products}
    rows = []
    subtotal = 0.0
    for item in items:
        qty = to_int(item.get("qty", 0))
        sku = str(item.get("sku", ""))
        product = product_records.get(sku)
        if not product:
            return jsonify({"error": f"Product {sku} was not found in the product catalog."}), 400
        if qty < 1 or to_int(product.get("Stock")) < qty:
            return jsonify({"error": f"Insufficient stock for {sku}. Available: {product.get('Stock', 0)}."}), 400
        price = to_float(product.get("Unit_Price", 0))
        line_total = qty * price
        subtotal += line_total
        rows.append([
            invoice_no, now_str, sku, product.get("Item_Name", ""),
            qty, price, line_total, payment_method,
        ])
    discount = min(discount, subtotal)
    remaining_discount = round(discount, 2)
    for index, row in enumerate(rows):
        if index == len(rows) - 1:
            line_discount = remaining_discount
        else:
            line_discount = round(discount * row[6] / subtotal, 2) if subtotal else 0
            remaining_discount = round(remaining_discount - line_discount, 2)
        row[6] = round(row[6] - line_discount, 2)
    ws.append_rows(rows, value_input_option="USER_ENTERED")
    for item in items:
        sku = str(item.get("sku", ""))
        for row_num, product in enumerate(products, start=2):
            if str(product.get("SKU", "")) == sku:
                new_stock = to_int(product.get("Stock")) - to_int(item.get("qty"))
                products_ws.update_cell(row_num, 5, new_stock)
                product["Stock"] = new_stock
                break

    invalidate_records(TAB_SALES, TAB_PRODUCTS)
    grand_total = round(subtotal - discount, 2)

    return jsonify({
        "invoice_no": invoice_no,
        "date_time": now_str,
        "items": items,
        "subtotal": round(subtotal, 2),
        "discount": discount,
        "grand_total": grand_total,
        "payment_method": payment_method,
    })


@app.route("/api/sales-list")
def sales_list():
    return jsonify(get_records(TAB_SALES))


@app.route("/api/sales-invoice/<path:invoice_no>", methods=["PUT", "DELETE"])
def sales_invoice(invoice_no):
    ws = get_sheet(TAB_SALES)
    records = get_records(TAB_SALES)
    matching = [(row_num, row) for row_num, row in enumerate(records, start=2)
                if str(row.get("Invoice_No", "")) == invoice_no]
    if not matching:
        return jsonify({"error": "Invoice was not found."}), 404
    if request.method == "PUT":
        data = request.get_json(force=True, silent=True) or {}
        payment = str(data.get("pay", "")).strip()
        if payment not in {"Cash", "Card", "Bank Transfer"}:
            return jsonify({"error": "Choose a valid payment method."}), 400
        for row_num, _ in matching:
            ws.update_cell(row_num, 8, payment)
        invalidate_records(TAB_SALES)
        return jsonify({"invoice_no": invoice_no, "payment_method": payment, "updated": True})

    quantities = {}
    for _, row in matching:
        sku = str(row.get("SKU_Barcode", ""))
        quantities[sku] = quantities.get(sku, 0) + to_int(row.get("Qty"))
    products_ws = get_products()
    products = get_records(TAB_PRODUCTS)
    product_rows = {str(product.get("SKU", "")): (row_num, product)
                    for row_num, product in enumerate(products, start=2)}
    for sku, qty in quantities.items():
        if sku in product_rows:
            row_num, product = product_rows[sku]
            products_ws.update_cell(row_num, 5, to_int(product.get("Stock")) + qty)
    for row_num, _ in reversed(matching):
        ws.delete_rows(row_num)
    invalidate_records(TAB_SALES, TAB_PRODUCTS)
    return jsonify({"invoice_no": invoice_no, "deleted": True, "stock_restored": True})


# --------------------------------------------------------------------------
# API: Fabric inventory
# --------------------------------------------------------------------------
@app.route("/api/add-fabric", methods=["POST"])
def add_fabric():
    data = request.get_json(force=True, silent=True) or {}
    ws = get_sheet(TAB_FABRIC)
    fabric_id = next_id("FAB", ws)

    meters = to_float(data.get("meters", 0))
    cost_per_meter = to_float(data.get("cost", 0))
    total_cost = round(meters * cost_per_meter, 2)
    date_str = datetime.now().strftime("%Y-%m-%d")

    ws.append_row(
        [fabric_id, data.get("name", ""), meters, cost_per_meter, total_cost, data.get("supplier", ""), date_str],
        value_input_option="USER_ENTERED",
    )
    invalidate_records(TAB_FABRIC)
    return jsonify({"fabric_id": fabric_id, "total_cost": total_cost})


@app.route("/api/fabric-record/<record_id>", methods=["PUT", "DELETE"])
def fabric_record(record_id):
    ws = get_sheet(TAB_FABRIC)
    records = get_records(TAB_FABRIC)
    for row_num, record in enumerate(records, start=2):
        if str(record.get("Fabric_ID", "")) == record_id:
            if request.method == "DELETE":
                ws.delete_rows(row_num)
                invalidate_records(TAB_FABRIC)
                return jsonify({"fabric_id": record_id, "deleted": True})
            data = request.get_json(force=True, silent=True) or {}
            name = str(data.get("name", "")).strip()
            meters = to_float(data.get("meters"), -1)
            cost = to_float(data.get("cost"), -1)
            if not name or meters < 0 or cost < 0:
                return jsonify({"error": "Enter a fabric name and valid non-negative meters and cost."}), 400
            ws.update(f"B{row_num}:F{row_num}", [[name, meters, cost, round(meters * cost, 2), str(data.get("supplier", "")).strip()]])
            invalidate_records(TAB_FABRIC)
            return jsonify({"fabric_id": record_id, "updated": True})
    return jsonify({"error": "Fabric record was not found."}), 404


@app.route("/api/fabric-list")
def fabric_list():
    ws = get_sheet(TAB_FABRIC)
    return jsonify(get_records(TAB_FABRIC))


def save_product_photo(upload, sku):
    allowed_extensions = {".jpg", ".jpeg", ".png", ".webp"}
    extension = os.path.splitext(secure_filename(upload.filename))[1].lower()
    if extension not in allowed_extensions:
        raise ValueError("Choose a JPG, PNG, or WEBP product photo.")
    upload_dir = Path(app.static_folder) / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{secure_filename(sku)}-{uuid.uuid4().hex[:10]}{extension}"
    upload.save(upload_dir / filename)
    return f"/static/uploads/{filename}"


@app.route("/api/products", methods=["GET", "POST", "PUT", "DELETE"])
def products_api():
    ws = get_products()
    if request.method == "GET":
        return jsonify([p for p in get_records(TAB_PRODUCTS) if str(p.get("Active", "TRUE")).upper() != "FALSE"])
    data = request.form.to_dict() if request.form else (request.get_json(force=True, silent=True) or {})
    sku = str(data.get("sku", "")).strip()
    records = get_records(TAB_PRODUCTS)
    if request.method == "DELETE":
        for row_num, product in enumerate(records, start=2):
            if str(product.get("SKU", "")).casefold() == sku.casefold():
                ws.update(f"F{row_num}", [["FALSE"]])
                invalidate_records(TAB_PRODUCTS)
                return jsonify({"sku": sku, "deleted": True})
        return jsonify({"error": "Product was not found."}), 404
    name = str(data.get("name", "")).strip()
    image_file = request.files.get("image")
    size = str(data.get("size", "")).strip()
    price = to_float(data.get("price"), -1)
    stock = to_int(data.get("stock"), -1)
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", sku) or not name or price < 0 or stock < 0:
        return jsonify({"error": "Use a 1-64 character SKU with letters, numbers, hyphens, dots, or underscores; enter a name, valid price, and stock."}), 400
    if request.method == "PUT":
        for row_num, product in enumerate(records, start=2):
            if str(product.get("SKU", "")).casefold() == sku.casefold():
                image_url = str(product.get("Image_URL", ""))
                if image_file and image_file.filename:
                    image_url = save_product_photo(image_file, sku)
                ws.update(f"A{row_num}:G{row_num}", [[sku, name, size, price, stock, "TRUE", image_url]])
                invalidate_records(TAB_PRODUCTS)
                return jsonify({"sku": sku, "image_url": image_url})
        return jsonify({"error": "Product was not found."}), 404
    if any(str(p.get("SKU", "")).casefold() == sku.casefold() for p in records):
        return jsonify({"error": "That SKU already exists."}), 409
    image_url = save_product_photo(image_file, sku) if image_file and image_file.filename else ""
    ws.append_row([sku, name, size, price, stock, "TRUE", image_url], value_input_option="USER_ENTERED")
    invalidate_records(TAB_PRODUCTS)
    return jsonify({"sku": sku, "image_url": image_url}), 201


# --------------------------------------------------------------------------
# API: Production log
# --------------------------------------------------------------------------
@app.route("/api/add-production", methods=["POST"])
def add_production():
    data = request.get_json(force=True, silent=True) or {}
    ws = get_sheet(TAB_PRODUCTION)
    production_id = next_id("PROD", ws)

    qty = to_int(data.get("qty", 0))
    meters = to_float(data.get("meters", 0))
    cost_per_piece = to_float(data.get("cost", 0))
    date_str = datetime.now().strftime("%Y-%m-%d")

    ws.append_row(
        [
            production_id, data.get("fabric_id", ""), data.get("item", ""), data.get("size", ""),
            qty, meters, cost_per_piece, date_str,
        ],
        value_input_option="USER_ENTERED",
    )
    invalidate_records(TAB_PRODUCTION)
    return jsonify({"production_id": production_id})


@app.route("/api/production-record/<record_id>", methods=["PUT", "DELETE"])
def production_record(record_id):
    ws = get_sheet(TAB_PRODUCTION)
    records = get_records(TAB_PRODUCTION)
    for row_num, record in enumerate(records, start=2):
        if str(record.get("Production_ID", "")) == record_id:
            if request.method == "DELETE":
                ws.delete_rows(row_num)
                invalidate_records(TAB_PRODUCTION)
                return jsonify({"production_id": record_id, "deleted": True})
            data = request.get_json(force=True, silent=True) or {}
            item = str(data.get("item", "")).strip()
            qty = to_int(data.get("qty"), -1)
            meters = to_float(data.get("meters"), -1)
            cost = to_float(data.get("cost"), -1)
            if not item or qty < 0 or meters < 0 or cost < 0:
                return jsonify({"error": "Enter an item and valid non-negative quantity, meters, and cost."}), 400
            ws.update(f"C{row_num}:G{row_num}", [[item, str(data.get("size", "")).strip(), qty, meters, cost]])
            invalidate_records(TAB_PRODUCTION)
            return jsonify({"production_id": record_id, "updated": True})
    return jsonify({"error": "Production record was not found."}), 404


@app.route("/api/production-list")
def production_list():
    return jsonify(get_records(TAB_PRODUCTION))


# --------------------------------------------------------------------------
# API: Expenses & wages
# --------------------------------------------------------------------------
@app.route("/api/add-expense", methods=["POST"])
def add_expense():
    data = request.get_json(force=True, silent=True) or {}
    ws = get_sheet(TAB_EXPENSES)
    expense_id = next_id("EXP", ws)

    amount = to_float(data.get("amt", 0))
    date_str = datetime.now().strftime("%Y-%m-%d")

    ws.append_row(
        [expense_id, data.get("cat", ""), amount, data.get("paid_to", ""), data.get("note", ""), date_str],
        value_input_option="USER_ENTERED",
    )
    invalidate_records(TAB_EXPENSES)
    return jsonify({"expense_id": expense_id})


@app.route("/api/expense-record/<record_id>", methods=["PUT", "DELETE"])
def expense_record(record_id):
    ws = get_sheet(TAB_EXPENSES)
    records = get_records(TAB_EXPENSES)
    for row_num, record in enumerate(records, start=2):
        if str(record.get("Expense_ID", "")) == record_id:
            if request.method == "DELETE":
                ws.delete_rows(row_num)
                invalidate_records(TAB_EXPENSES)
                return jsonify({"expense_id": record_id, "deleted": True})
            data = request.get_json(force=True, silent=True) or {}
            category = str(data.get("cat", "")).strip()
            amount = to_float(data.get("amt"), -1)
            if not category or amount < 0:
                return jsonify({"error": "Enter an expense category and a valid non-negative amount."}), 400
            ws.update(f"B{row_num}:E{row_num}", [[category, amount, str(data.get("paid_to", "")).strip(), str(data.get("note", "")).strip()]])
            invalidate_records(TAB_EXPENSES)
            return jsonify({"expense_id": record_id, "updated": True})
    return jsonify({"error": "Expense record was not found."}), 404


@app.route("/api/expenses-list")
def expenses_list():
    return jsonify(get_records(TAB_EXPENSES))


# --------------------------------------------------------------------------
# API: Dashboard metrics
# --------------------------------------------------------------------------
@app.route("/api/dashboard-metrics")
def dashboard_metrics():
    sales_records = get_records(TAB_SALES)
    fabric_records = get_records(TAB_FABRIC)
    expenses_records = get_records(TAB_EXPENSES)
    production_records = get_records(TAB_PRODUCTION)

    total_revenue = sum(to_float(r.get("Total")) for r in sales_records)
    fabric_investment = sum(to_float(r.get("Total_Cost")) for r in fabric_records)
    direct_expenses = sum(to_float(r.get("Amount")) for r in expenses_records)
    production_cost = sum(
        to_float(r.get("Cost_Per_Piece")) * to_float(r.get("Qty_Produced")) for r in production_records
    )
    total_expenses = round(direct_expenses + production_cost, 2)
    net_profit = round(total_revenue - fabric_investment - total_expenses, 2)

    return jsonify({
        "total_revenue": round(total_revenue, 2),
        "fabric_investment": round(fabric_investment, 2),
        "total_expenses": total_expenses,
        "net_profit": net_profit,
        "sales_count": len({r.get("Invoice_No") for r in sales_records if r.get("Invoice_No")}),
        "fabric_count": len(fabric_records),
        "production_count": len(production_records),
        "expenses_count": len(expenses_records),
    })


@app.route("/api/analytics")
def analytics():
    try:
        days = int(request.args.get("days", 30))
    except (TypeError, ValueError):
        days = 30
    if days not in (7, 30, 90):
        days = 30

    today = datetime.now().date()
    first_day = today - timedelta(days=days - 1)
    dates = [first_day + timedelta(days=i) for i in range(days)]
    daily_sales = {d.isoformat(): 0.0 for d in dates}
    daily_costs = {d.isoformat(): 0.0 for d in dates}
    categories = {}
    top_items = {}

    for row in get_records(TAB_SALES):
        raw_date = str(row.get("Date_Time", ""))[:10]
        if raw_date in daily_sales:
            amount = to_float(row.get("Total"))
            daily_sales[raw_date] += amount
            name = str(row.get("Item_Name", "Other"))
            item = top_items.setdefault(name, {"name": name, "qty": 0, "sales": 0.0})
            item["qty"] += to_int(row.get("Qty"))
            item["sales"] += amount

    for row in get_records(TAB_EXPENSES):
        raw_date = str(row.get("Date", ""))[:10]
        if raw_date in daily_costs:
            amount = to_float(row.get("Amount"))
            daily_costs[raw_date] += amount
            category = str(row.get("Category") or "Other")
            categories[category] = categories.get(category, 0.0) + amount

    for row in get_records(TAB_PRODUCTION):
        raw_date = str(row.get("Date", ""))[:10]
        if raw_date in daily_costs:
            cost = to_float(row.get("Cost_Per_Piece")) * to_float(row.get("Qty_Produced"))
            daily_costs[raw_date] += cost
            categories["Production"] = categories.get("Production", 0.0) + cost

    return jsonify({
        "days": days,
        "labels": [d.strftime("%d %b") for d in dates],
        "dates": [d.isoformat() for d in dates],
        "revenue": [round(daily_sales[d.isoformat()], 2) for d in dates],
        "expenses": [round(daily_costs[d.isoformat()], 2) for d in dates],
        "expense_categories": [{"name": k, "amount": round(v, 2)} for k, v in sorted(categories.items(), key=lambda item: item[1], reverse=True)],
        "top_products": sorted(top_items.values(), key=lambda item: item["sales"], reverse=True)[:8],
    })


@app.errorhandler(404)
def handle_not_found(err):
    if request.path.startswith("/api/"):
        return jsonify({"error": "API route not found. Restart the app to load the latest routes."}), 404
    return err


@app.errorhandler(413)
def handle_upload_too_large(err):
    return jsonify({"error": "Product photos must be 5 MB or smaller."}), 413


@app.errorhandler(ValueError)
def handle_invalid_upload(err):
    return jsonify({"error": str(err)}), 400


@app.errorhandler(gspread.exceptions.APIError)
def handle_sheets_api_error(err):
    response = getattr(err, "response", None)
    if getattr(response, "status_code", None) == 429:
        return jsonify({"error": "Google Sheets is temporarily rate-limiting requests. Please wait a few seconds and try again."}), 503
    return jsonify({"error": "Google Sheets could not complete the request. Check the server log."}), 502


@app.errorhandler(FileNotFoundError)
def handle_missing_credentials(err):
    return jsonify({"error": str(err)}), 500


@app.errorhandler(Exception)
def handle_unexpected_error(err):
    if isinstance(err, HTTPException):
        return err
    app.logger.exception("Request failed")
    return jsonify({"error": "The request could not be completed. Check the server log and Google Sheets access."}), 500


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if request.path == "/" or request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    if APP_ENV == "production":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


if __name__ == "__main__":
    host = APP_HOST
    port = int(os.environ.get("PORT", "5000"))
    if APP_ENV == "production":
        from waitress import serve
        serve(app, host=host, port=port, threads=8)
    else:
        app.run(debug=False, host=host, port=port)
