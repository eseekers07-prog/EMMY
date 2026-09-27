# EMMY Cloths POS — Setup Guide

This app is a Flask backend + single-page frontend that reads and writes to a
Google Sheet acting as your database. Follow these steps in order.

## 1. Create the Google Sheet

1. Go to [Google Sheets](https://sheets.google.com) and create a new spreadsheet.
2. Rename it exactly: **`EMMY_Cloths_POS_Database`**
   (or pick your own name and set `EMMY_SPREADSHEET_NAME` in step 5).
3. The app will auto-create the tabs below (with headers) the first time
   it writes to each one, so you don't have to build them by hand. If you'd
   rather set them up yourself, create these tabs with these exact headers in row 1:

   **Fabric_Inventory**
   `Fabric_ID | Fabric_Type | Roll_Meters | Cost_Per_Meter | Total_Cost | Supplier | Date`

   **Production_Log**
   `Production_ID | Fabric_ID | Design_Name | Size | Qty_Produced | Meters_Used | Cost_Per_Piece | Date`

   **Sales_Transactions**
   `Invoice_No | Date_Time | SKU_Barcode | Item_Name | Qty | Unit_Price | Total | Payment_Method`

   **Expenses_Salaries**
   `Expense_ID | Category | Amount | Paid_To | Notes | Date`

   **Products** (created by the app; the current sample catalog is inserted if the tab is empty)
   `SKU | Item_Name | Size | Unit_Price | Stock | Active | Image_URL`

   Add or update products in the app's **Products & Stock** page. Completed sales are recorded in `Sales_Transactions` and deduct the sold quantity from product stock. Product photos uploaded in the app are saved under `static/uploads`; their paths are recorded in the Products sheet's `Image_URL` column. Include that folder when backing up or moving the app.

## 2. Create a Google Cloud service account

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and create
   (or select) a project.
2. Enable two APIs for that project: **Google Sheets API** and **Google Drive API**
   (APIs & Services → Library → search each → Enable).
3. Go to **APIs & Services → Credentials → Create Credentials → Service Account**.
   Give it any name (e.g. `emmy-pos-service`) and finish the wizard.
4. Open the new service account → **Keys** tab → **Add Key → Create new key → JSON**.
   A `.json` file downloads — this is your credential file.
5. Rename it `service_account.json` and place it in the same folder as `app.py`
   (or point `GOOGLE_SERVICE_ACCOUNT_FILE` at wherever you keep it).

## 3. Share the spreadsheet with the service account

1. Open the downloaded JSON file and copy the `client_email` value
   (looks like `emmy-pos-service@your-project.iam.gserviceaccount.com`).
2. In your `EMMY_Cloths_POS_Database` spreadsheet, click **Share** and paste
   that email in as an **Editor**. Without this step the app cannot read or
   write any data — you'll get a "permission denied" / "spreadsheet not found"
   error.

## 4. Install dependencies

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 5. Configure environment variables (optional)

By default the app looks for `service_account.json` in its own folder and a
spreadsheet named `EMMY_Cloths_POS_Database`. To override either:

```bash
export GOOGLE_SERVICE_ACCOUNT_FILE=/path/to/your-key.json
export EMMY_SPREADSHEET_NAME="EMMY_Cloths_POS_Database"
```

(On Windows, use `set VAR=value` instead of `export`.)

## 6. Run the app

```bash
python app.py
```

Visit **http://localhost:5000** in your browser. The barcode/search box is
auto-focused for scanner input, and every action (checkout, add fabric, log
production, add expense) writes straight to the corresponding tab in your
Google Sheet — the dashboard cards at the top refresh automatically after
each action.

## 7. Product catalog

Manage SKUs, names, sizes, prices, stock, and product photos (JPG, PNG, WEBP up to 5 MB) in the app's **Products & Stock** tab. If the Products tab is empty, the app inserts the built-in sample catalog once; update those entries to match your inventory. Use **Generate SKU** when adding an item; that value is also its Code 128 barcode. Use **View / Print** for a label. In POS Billing, scan the label into the SKU/barcode box; configure the scanner to send Enter after each scan.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Service account key not found` | `service_account.json` isn't in the app folder, or `GOOGLE_SERVICE_ACCOUNT_FILE` points to the wrong path. |
| `SpreadsheetNotFound` | The sheet name doesn't match `EMMY_SPREADSHEET_NAME`, or it hasn't been shared with the service account's email. |
| `PermissionError` / 403 from Google | The service account wasn't given Editor access on the sheet, or the Sheets/Drive APIs aren't enabled on the project. |
| Data isn't refreshing after checkout | Check the browser console/network tab — the frontend calls `/api/dashboard-metrics` after every write; a 500 there usually means one of the two errors above. |


## Production deployment notes

For public use, deploy behind a hosting provider or reverse proxy that provides
HTTPS. Set these variables in the host's secret/configuration settings; do not
put real secrets in source code:

- APP_ENV=production
- APP_HOST=0.0.0.0
- APP_USERNAME (owner login name)
- APP_PASSWORD (unique password, at least 16 characters)
- SECRET_KEY (generate with: python -c "import secrets; print(secrets.token_hex(32))")
- GOOGLE_SERVICE_ACCOUNT_FILE (path to the key mounted as a secret)
- EMMY_SPREADSHEET_NAME=EMMY_Cloths_POS_Database

Production refuses to start without APP_PASSWORD and SECRET_KEY. It uses
Waitress with debug mode disabled, secure session cookies, a 12-hour login
session, and CSRF checks on write requests. Check /healthz for deployment
health. Back up the Google Sheet and static/uploads product photos.

The included service_account.json path is intended for local development. For
public deployment, put the key in the hosting provider's secret manager and set
GOOGLE_SERVICE_ACCOUNT_FILE to its mounted path. The .gitignore excludes local
credentials, environment files, virtual environments, and uploaded photos from
future Git additions. If a key was ever committed to a repository or shared
publicly, revoke it in Google Cloud and create a new one.
