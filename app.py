"""
Osmos Feed Sync — Streamlit Multi-Page UI

Page 1 — Feed Sync V2:  full feed, all fields, no filtering
Page 2 — Multi-Language: text fields only + language ISO code
Page 3 — cURL Generator: get full cURL for any single SKU

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
        ok = _post(session, API_FEED_V2, {"products": batch}, headers, result, f"Batch {idx+1}")
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
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(page_title="Osmos Feed Sync", page_icon="🔄", layout="wide")

# ── Sidebar (shared across all pages) ──
with st.sidebar:
    st.header("API Credentials")
    retailer_id = st.text_input("Retailer ID (x-retailer-id)", type="default",
                                help="Agency ID from Osmos developer settings")
    token = st.text_input("API Token (x-token)", type="password",
                          help="Token from Osmos developer settings")

    st.divider()
    st.header("Settings")
    st.caption(f"Batch: {BATCH_SIZE} | Rate: {RATE_LIMIT} req/s | Retries: {RETRY_ATTEMPTS}")

has_creds = bool(retailer_id and token)
remap = DEFAULT_REMAP

# ── Navigation ──
page = st.radio(
    "Navigate",
    ["1 — Feed Sync V2", "2 — Multi-Language Sync", "3 — cURL Generator"],
    horizontal=True,
    label_visibility="collapsed",
)

st.markdown("---")

# =====================================================================
# PAGE 1 — Feed Sync V2
# =====================================================================
if page == "1 — Feed Sync V2":
    st.header("Step 1 — Feed Sync V2")
    st.caption("Upload the **full product feed**. All columns are sent as-is to the Feed V2 API — nothing is filtered or removed.")

    v2_file = st.file_uploader("Upload feed file (CSV / TSV)", type=["csv", "tsv"], key="v2_file")

    if v2_file:
        v2_fieldnames, v2_rows = parse_csv(v2_file)
        v2_products, v2_skipped = extract_products(v2_rows, remap)

        # Store in session for cURL page
        st.session_state["v2_products"] = v2_products
        st.session_state["v2_filename"] = v2_file.name

        col_info, col_fields = st.columns([1, 1])
        with col_info:
            st.metric("Products Ready", f"{len(v2_products):,}")
        with col_fields:
            st.metric("Fields per Product", len(v2_fieldnames))

        if v2_skipped:
            st.warning(f"{v2_skipped} rows skipped (missing required fields: id, title, description, brand)")

        with st.expander("Preview payload — first product"):
            if v2_products:
                st.json(v2_products[0])

        st.markdown("---")

        col1, col2 = st.columns(2)
        with col1:
            v2_dry = st.button("Dry Run", key="v2_dry", use_container_width=True)
        with col2:
            v2_sync = st.button("Sync Feed V2", key="v2_sync", type="primary",
                                use_container_width=True, disabled=not has_creds)

        if not has_creds:
            st.caption("Enter API credentials in the sidebar to enable sync.")

        if v2_dry and v2_products:
            n_batches = (len(v2_products) + BATCH_SIZE - 1) // BATCH_SIZE
            st.info(f"**Dry Run:** {len(v2_products):,} products → {n_batches} batches → Feed V2 API")
            st.caption("Sample payload (first product):")
            st.code(json.dumps({"products": [v2_products[0]]}, ensure_ascii=False, indent=2), language="json")

        if v2_sync:
            if not v2_products:
                st.warning("No valid products to sync.")
            else:
                st.markdown("### Sync Progress")
                progress = st.progress(0, text="Starting Feed V2 sync...")
                status = st.empty()
                result = run_v2_sync(retailer_id, token, v2_products, progress, status)

                if result.failed == 0:
                    st.success(f"**{result.synced:,}/{result.total:,}** products synced ✅")
                    st.balloons()
                else:
                    st.error(f"{result.synced:,} synced, {result.failed:,} failed ❌")

                with st.expander("Sync logs"):
                    for line in result.logs:
                        st.text(line)

                st.info("👉 Next: switch to **Step 2 — Multi-Language Sync** above to sync the translated feed.")


# =====================================================================
# PAGE 2 — Multi-Language Sync
# =====================================================================
elif page == "2 — Multi-Language Sync":
    st.header("Step 2 — Multi-Language Sync")
    st.caption(
        "Upload the **translated feed**. Only text fields are sent to the Multi-Language API with the language code."
    )

    ml_file = st.file_uploader("Upload translated feed file (CSV / TSV)", type=["csv", "tsv"], key="ml_file")

    col_lang, col_info = st.columns([1, 2])
    with col_lang:
        lang_keys = list(LANGUAGE_OPTIONS.keys())
        ml_lang = st.selectbox(
            "Language ISO code",
            options=lang_keys,
            format_func=lambda k: f"{k} — {LANGUAGE_OPTIONS[k]}",
            index=0,
            key="ml_lang",
        )
    with col_info:
        ml_text_fields = sorted(ML_FIELDS - {"id"})
        st.caption(f"**Fields sent:** `{'`, `'.join(ml_text_fields)}`")

    if ml_file:
        ml_fieldnames, ml_rows = parse_csv(ml_file)
        ml_products_raw, ml_skipped = extract_products(ml_rows, remap)
        ml_products_clean = [ml_only(p) for p in ml_products_raw]

        # Store in session for cURL page
        st.session_state["ml_products_raw"] = ml_products_raw
        st.session_state["ml_products_clean"] = ml_products_clean
        st.session_state["ml_filename"] = ml_file.name
        st.session_state["ml_lang_val"] = ml_lang

        col_m1, col_m2 = st.columns([1, 1])
        with col_m1:
            st.metric("Products Ready", f"{len(ml_products_clean):,}")
        with col_m2:
            fields_present = sorted(ml_products_clean[0].keys()) if ml_products_clean else []
            st.metric("ML Fields Found", len(fields_present))

        if ml_skipped:
            st.warning(f"{ml_skipped} rows skipped (missing required fields)")

        with st.expander("Preview ML payload — first product"):
            if ml_products_clean:
                st.json({"language": ml_lang, "products": [ml_products_clean[0]]})

        st.markdown("---")

        col3, col4 = st.columns(2)
        with col3:
            ml_dry = st.button("Dry Run", key="ml_dry", use_container_width=True)
        with col4:
            ml_sync = st.button("Sync Multi-Language", key="ml_sync", type="primary",
                                use_container_width=True, disabled=not has_creds)

        if not has_creds:
            st.caption("Enter API credentials in the sidebar to enable sync.")

        if ml_dry and ml_products_raw:
            n_batches = (len(ml_products_raw) + BATCH_SIZE - 1) // BATCH_SIZE
            st.info(f"**Dry Run:** {len(ml_products_raw):,} products → {n_batches} batches → "
                    f"Multi-Language API (language=`{ml_lang}`)")
            st.caption("Sample ML payload (first product):")
            st.code(json.dumps({"language": ml_lang, "products": [ml_products_clean[0]]},
                               ensure_ascii=False, indent=2), language="json")

        if ml_sync:
            if not ml_products_raw:
                st.warning("No valid products to sync.")
            else:
                st.markdown("### Sync Progress")
                progress = st.progress(0, text=f"Starting ML sync (language={ml_lang})...")
                status = st.empty()
                result = run_ml_sync(retailer_id, token, ml_lang, ml_products_raw, progress, status)

                if result.failed == 0:
                    st.success(f"**{result.synced:,}/{result.total:,}** products synced ✅  (language=`{ml_lang}`)")
                    st.balloons()
                else:
                    st.error(f"{result.synced:,} synced, {result.failed:,} failed ❌")

                with st.expander("Sync logs"):
                    for line in result.logs:
                        st.text(line)


# =====================================================================
# PAGE 3 — cURL Generator
# =====================================================================
elif page == "3 — cURL Generator":
    st.header("cURL Generator")
    st.caption("Generate copy-pasteable cURL commands for any single SKU from uploaded feeds.")

    v2_products = st.session_state.get("v2_products", [])
    ml_products_raw = st.session_state.get("ml_products_raw", [])
    ml_products_clean = st.session_state.get("ml_products_clean", [])
    ml_lang = st.session_state.get("ml_lang_val", "ar")

    if not v2_products and not ml_products_clean:
        st.info("Upload feeds in **Step 1** and/or **Step 2** first — then come here to generate cURLs.")
        st.stop()

    # Build combined SKU list
    all_ids = set()
    v2_by_id = {}
    ml_by_id = {}
    for p in v2_products:
        all_ids.add(p["id"])
        v2_by_id[p["id"]] = p
    for p_raw, p_clean in zip(ml_products_raw, ml_products_clean):
        all_ids.add(p_raw["id"])
        ml_by_id[p_raw["id"]] = p_clean

    sku_list = sorted(all_ids)
    selected_sku = st.selectbox("Select SKU (product id)", sku_list, key="curl_sku")

    if selected_sku:
        has_v2 = selected_sku in v2_by_id
        has_ml = selected_sku in ml_by_id
        badges = []
        if has_v2:
            badges.append("Feed V2")
        if has_ml:
            badges.append(f"Multi-Language ({ml_lang})")
        st.caption(f"**SKU `{selected_sku}`** available in: {' · '.join(badges)}")

        if has_v2:
            st.markdown("### Feed V2 cURL")
            prod = v2_by_id[selected_sku]
            st.code(_make_curl(API_FEED_V2, retailer_id, token, {"products": [prod]}), language="bash")

        if has_ml:
            st.markdown(f"### Multi-Language cURL (language=`{ml_lang}`)")
            prod = ml_by_id[selected_sku]
            st.code(
                _make_curl(API_MULTI_LANG, retailer_id, token,
                           {"language": ml_lang, "products": [prod]}),
                language="bash",
            )
