"""
Osmos Feed Sync — Streamlit UI

Two-step sync:
  Step 1 — Feed Sync V2 (/products)  →  full feed, all fields, no filtering
  Step 2 — Multi-Language (/products/multiLanguage) →  text fields only + language ISO code

Run:  streamlit run app.py
"""

import csv
import io
import json
import re
import time
import unicodedata
from dataclasses import dataclass, field

import requests
import streamlit as st

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

API_FEED_V2 = "https://apiv2.onlinesales.ai/catalogSyncService/products"
API_MULTI_LANG = "https://apiv2.onlinesales.ai/catalogSyncService/products/multiLanguage"

BATCH_SIZE = 50
RATE_LIMIT = 10
RETRY_ATTEMPTS = 3
RETRY_DELAY = 2

# Multi-Language API accepted fields
ML_FIELDS = {"id", "title", "description", "brand", "category",
             "custom_label_0", "custom_label_1", "custom_label_2",
             "secondary_categories"}

REQUIRED_FIELDS = {"id", "title", "description", "brand"}

DEFAULT_REMAP = {
    "image_url": "image_link",
}

LANGUAGE_OPTIONS = {
    "ar": "Arabic (العربية)",
    "fr": "French (Français)",
    "de": "German (Deutsch)",
    "es": "Spanish (Español)",
    "tr": "Turkish (Türkçe)",
    "pt": "Portuguese (Português)",
    "zh": "Chinese (中文)",
    "ja": "Japanese (日本語)",
    "ko": "Korean (한국어)",
    "hi": "Hindi (हिन्दी)",
    "ur": "Urdu (اردو)",
    "en": "English",
}

_STRIP_CHARS = str.maketrans("", "", "\r\n\0\t")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SyncResult:
    label: str
    total: int = 0
    synced: int = 0
    failed: int = 0
    skipped: int = 0
    logs: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def parse_csv(uploaded_file) -> tuple[list[str], list[dict]]:
    raw = uploaded_file.getvalue()
    text = raw.decode("utf-8-sig")
    delimiter = "\t" if uploaded_file.name.endswith(".tsv") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    rows = list(reader)
    return list(reader.fieldnames or []), rows


def remap_row(row: dict, remap: dict) -> dict:
    out = {}
    for col, raw in row.items():
        if col is None:
            continue
        api_name = remap.get(col, col)
        value = (raw or "").strip().translate(_STRIP_CHARS)
        if value:
            out[api_name] = value
    return out


def extract_products(rows: list[dict], remap: dict) -> tuple[list[dict], int]:
    products, skipped = [], 0
    for row in rows:
        mapped = remap_row(row, remap)
        if REQUIRED_FIELDS - mapped.keys():
            skipped += 1
            continue
        products.append(mapped)
    return products, skipped


def _clean_text(value: str) -> str:
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Cc")
    value = re.sub(r"[​-‏‪-‮⁦-⁩﻿؜]", "", value)
    value = re.sub(r" {2,}", " ", value).strip()
    return value


def ml_only(product: dict) -> dict:
    out = {}
    for k, v in product.items():
        if k in ML_FIELDS:
            out[k] = _clean_text(v) if k != "id" else v
    return out


def _post(session, endpoint, payload, headers, result, label, retry_attempts=RETRY_ATTEMPTS):
    for attempt in range(1, retry_attempts + 1):
        try:
            resp = session.post(endpoint, json=payload, headers=headers, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                result.logs.append(f"[{label}] OK  request_id={data.get('request_id', '?')}")
                return True
            try:
                body = resp.json()
                error = body.get("error", {})
                msg = (f"[{label}] FAIL  attempt={attempt}/{retry_attempts}  "
                       f"http={resp.status_code}  code={error.get('code')}  "
                       f"msg={error.get('message')}  full={json.dumps(body)[:300]}")
            except Exception:
                msg = (f"[{label}] FAIL  attempt={attempt}/{retry_attempts}  "
                       f"http={resp.status_code}  body={resp.text[:300]}")
            result.logs.append(msg)
        except requests.RequestException as exc:
            result.logs.append(f"[{label}] ERROR  attempt={attempt}/{retry_attempts}  {exc}")
        if attempt < retry_attempts:
            time.sleep(RETRY_DELAY)
    return False


def _make_curl(endpoint, retailer_id, token, payload_dict):
    body = json.dumps(payload_dict, ensure_ascii=False, indent=2)
    return (
        f"curl -X POST '{endpoint}' \\\n"
        f"  -H 'Content-Type: application/json' \\\n"
        f"  -H 'x-retailer-id: {retailer_id}' \\\n"
        f"  -H 'x-token: {token}' \\\n"
        f"  -d '{body}'"
    )


# ---------------------------------------------------------------------------
# Sync runners
# ---------------------------------------------------------------------------

def run_v2_sync(retailer_id, token, products, progress_bar, status_text):
    result = SyncResult(label="Feed V2", total=len(products))
    headers = {"x-retailer-id": retailer_id, "x-token": token, "Content-Type": "application/json"}
    session = requests.Session()
    min_interval = 1.0 / RATE_LIMIT
    batches = [products[i:i + BATCH_SIZE] for i in range(0, len(products), BATCH_SIZE)]

    for idx, batch in enumerate(batches):
        if idx > 0:
            time.sleep(min_interval)
        payload = {"products": batch}
        ok = _post(session, API_FEED_V2, payload, headers, result, f"Batch {idx+1}")
        if ok:
            result.synced += len(batch)
        else:
            result.failed += len(batch)
        progress_bar.progress((idx + 1) / len(batches), text=f"Batch {idx+1}/{len(batches)}")
        status_text.text(f"Synced {result.synced}/{result.total} products")

    return result


def run_ml_sync(retailer_id, token, language, products, progress_bar, status_text):
    result = SyncResult(label=f"Multi-Language ({language})", total=len(products))
    headers = {"x-retailer-id": retailer_id, "x-token": token, "Content-Type": "application/json"}
    session = requests.Session()
    min_interval = 1.0 / RATE_LIMIT
    batches = [products[i:i + BATCH_SIZE] for i in range(0, len(products), BATCH_SIZE)]

    for idx, batch in enumerate(batches):
        if idx > 0:
            time.sleep(max(min_interval, 1.0))
        ml_batch = [ml_only(p) for p in batch]
        payload = {"language": language, "products": ml_batch}
        result.logs.append(f"[Batch {idx+1}] Sending {len(ml_batch)} products, "
                           f"fields={sorted(ml_batch[0].keys()) if ml_batch else '?'}")
        ok = _post(session, API_MULTI_LANG, payload, headers, result, f"Batch {idx+1}")
        if ok:
            result.synced += len(batch)
        else:
            result.failed += len(batch)
        progress_bar.progress((idx + 1) / len(batches), text=f"Batch {idx+1}/{len(batches)}")
        status_text.text(f"Synced {result.synced}/{result.total} products")

    return result


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------

st.set_page_config(page_title="Osmos Feed Sync", page_icon="🔄", layout="wide")
st.title("Osmos Feed Sync")

# ── Sidebar ──
with st.sidebar:
    st.header("API Credentials")
    retailer_id = st.text_input("Retailer ID (x-retailer-id)", type="default",
                                help="Agency ID from Osmos developer settings")
    token = st.text_input("API Token (x-token)", type="password",
                          help="Token from Osmos developer settings")

    st.divider()
    st.header("Settings")
    st.caption(f"Batch: {BATCH_SIZE} | Rate: {RATE_LIMIT} req/s | Retries: {RETRY_ATTEMPTS}")

    st.divider()
    st.header("Column Remapping")
    st.caption("Rename CSV columns to match API field names")
    custom_remap = {}
    for csv_col, api_col in DEFAULT_REMAP.items():
        val = st.text_input(f"`{csv_col}` →", value=api_col, key=f"remap_{csv_col}")
        if val:
            custom_remap[csv_col] = val
    remap = {**DEFAULT_REMAP, **custom_remap}

has_creds = bool(retailer_id and token)

# =====================================================================
# STEP 1 — Feed Sync V2 (full feed, all fields)
# =====================================================================
st.header("Step 1 — Feed Sync V2")
st.caption("Upload the **full product feed**. All columns are sent as-is to the Feed V2 API — nothing is filtered.")

v2_file = st.file_uploader("Upload feed file (CSV / TSV)", type=["csv", "tsv"], key="v2_file")

if v2_file:
    v2_fieldnames, v2_rows = parse_csv(v2_file)
    v2_products, v2_skipped = extract_products(v2_rows, remap)

    st.success(f"**{v2_file.name}** — {len(v2_products):,} products ready · {v2_skipped} skipped · {len(v2_fieldnames)} fields")

    with st.expander("Preview payload (first product)"):
        if v2_products:
            st.json(v2_products[0])

    # ── cURL ──
    with st.expander("cURL for single SKU"):
        if v2_products:
            sku_ids_v2 = [p["id"] for p in v2_products]
            sel_v2 = st.selectbox("Select SKU", sku_ids_v2, key="v2_sku")
            prod_v2 = next(p for p in v2_products if p["id"] == sel_v2)
            st.code(_make_curl(API_FEED_V2, retailer_id, token, {"products": [prod_v2]}), language="bash")

    # ── Sync button ──
    col1, col2 = st.columns(2)
    with col1:
        v2_dry = st.button("Dry Run", key="v2_dry", use_container_width=True)
    with col2:
        v2_sync = st.button("Sync Feed V2", key="v2_sync", type="primary",
                            use_container_width=True, disabled=not has_creds)

    if v2_dry and v2_products:
        st.info(f"**Dry Run:** {len(v2_products):,} products would be sent in "
                f"{(len(v2_products) + BATCH_SIZE - 1) // BATCH_SIZE} batches.")
        st.code(json.dumps(v2_products[0], ensure_ascii=False, indent=2), language="json")

    if v2_sync:
        if not has_creds:
            st.error("Enter credentials in the sidebar.")
        elif not v2_products:
            st.warning("No valid products to sync.")
        else:
            progress = st.progress(0, text="Starting Feed V2 sync...")
            status = st.empty()
            result = run_v2_sync(retailer_id, token, v2_products, progress, status)
            if result.failed == 0:
                st.success(f"Feed V2: **{result.synced:,}/{result.total:,}** products synced ✅")
            else:
                st.error(f"Feed V2: {result.synced:,} synced, {result.failed:,} failed ❌")
            with st.expander("Sync logs"):
                for line in result.logs:
                    st.text(line)

st.divider()

# =====================================================================
# STEP 2 — Multi-Language Sync (text fields only + language code)
# =====================================================================
st.header("Step 2 — Multi-Language Sync")
st.caption(
    "Upload the **translated feed**. Only text fields "
    f"(`{'`, `'.join(sorted(ML_FIELDS - {'id'}))}`) are sent to the Multi-Language API with the language code."
)

ml_file = st.file_uploader("Upload translated feed file (CSV / TSV)", type=["csv", "tsv"], key="ml_file")

col_lang, _ = st.columns([1, 2])
with col_lang:
    lang_keys = list(LANGUAGE_OPTIONS.keys())
    ml_lang = st.selectbox(
        "Language ISO code",
        options=lang_keys,
        format_func=lambda k: f"{k} — {LANGUAGE_OPTIONS[k]}",
        index=0,
        key="ml_lang",
    )

if ml_file:
    ml_fieldnames, ml_rows = parse_csv(ml_file)
    ml_products_raw, ml_skipped = extract_products(ml_rows, remap)
    ml_products_clean = [ml_only(p) for p in ml_products_raw]

    st.success(
        f"**{ml_file.name}** — {len(ml_products_clean):,} products ready · {ml_skipped} skipped · "
        f"Language: **{ml_lang}** ({LANGUAGE_OPTIONS.get(ml_lang, ml_lang)})"
    )

    fields_present = sorted(ml_products_clean[0].keys()) if ml_products_clean else []
    st.caption(f"Fields sent: `{'`, `'.join(fields_present)}`")

    with st.expander("Preview ML payload (first product)"):
        if ml_products_clean:
            st.json({"language": ml_lang, "products": [ml_products_clean[0]]})

    # ── cURL ──
    with st.expander("cURL for single SKU"):
        if ml_products_clean:
            sku_ids_ml = [p["id"] for p in ml_products_clean]
            sel_ml = st.selectbox("Select SKU", sku_ids_ml, key="ml_sku")
            prod_ml = next(p for p in ml_products_clean if p["id"] == sel_ml)
            st.code(
                _make_curl(API_MULTI_LANG, retailer_id, token,
                           {"language": ml_lang, "products": [prod_ml]}),
                language="bash",
            )

    # ── Sync button ──
    col3, col4 = st.columns(2)
    with col3:
        ml_dry = st.button("Dry Run", key="ml_dry", use_container_width=True)
    with col4:
        ml_sync = st.button("Sync Multi-Language", key="ml_sync", type="primary",
                            use_container_width=True, disabled=not has_creds)

    if ml_dry and ml_products_raw:
        st.info(f"**Dry Run:** {len(ml_products_raw):,} products would be sent in "
                f"{(len(ml_products_raw) + BATCH_SIZE - 1) // BATCH_SIZE} batches "
                f"with language=`{ml_lang}`.")
        st.code(json.dumps({"language": ml_lang, "products": [ml_products_clean[0]]},
                           ensure_ascii=False, indent=2), language="json")

    if ml_sync:
        if not has_creds:
            st.error("Enter credentials in the sidebar.")
        elif not ml_products_raw:
            st.warning("No valid products to sync.")
        else:
            progress = st.progress(0, text=f"Starting ML sync (language={ml_lang})...")
            status = st.empty()
            result = run_ml_sync(retailer_id, token, ml_lang, ml_products_raw, progress, status)
            if result.failed == 0:
                st.success(f"Multi-Language ({ml_lang}): **{result.synced:,}/{result.total:,}** products synced ✅")
            else:
                st.error(f"Multi-Language ({ml_lang}): {result.synced:,} synced, {result.failed:,} failed ❌")
            with st.expander("Sync logs"):
                for line in result.logs:
                    st.text(line)
