"""
Osmos Operations Hub — Streamlit UI

Left sidebar: tool selector + API credentials
Main area:    Feed Sync  |  Advertiser & Wallet management

Run:  streamlit run app.py
"""

import base64
import csv
import io
import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field

import requests
import streamlit as st
from streamlit.components.v1 import html as st_html

# ---------------------------------------------------------------------------
# Sound effects — custom MP3 embedded as base64
# ---------------------------------------------------------------------------

_SOUND_DIR = os.path.dirname(os.path.abspath(__file__))


@st.cache_data
def _load_success_sound_b64() -> str:
    path = os.path.join(_SOUND_DIR, "success_sound.mp3")
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def _play_audio_js(b64: str) -> str:
    return f"""
<script>
(function(){{
  const audio = new Audio("data:audio/mp3;base64,{b64}");
  audio.volume = 1.0;
  audio.play();
}})();
</script>
"""


def play_success():
    st_html(_play_audio_js(_load_success_sound_b64()), height=0)


# ---------------------------------------------------------------------------
# Constants — Feed Sync
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
DEFAULT_REMAP = {"image_url": "image_link"}

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
# Constants — Advertiser & Wallet
# ---------------------------------------------------------------------------

API_ADVERTISER_CREATE = "https://apiv2.onlinesales.ai/marketing/v1/advertiser/create"
API_ADVERTISER_GET = "https://apiv2.onlinesales.ai/marketing/v1/advertiser/"
API_WALLET_BASE = "https://apiv2.onlinesales.ai/billing/v1/advertiser/{advertiser_id}/wallet"
API_WALLET_BY_ID = "https://apiv2.onlinesales.ai/billing/v1/advertiser/{advertiser_id}/wallet/{wallet_id}"
API_TRANSACTION_CREATE = "https://apiv2.onlinesales.ai/billing/v1/advertiser/{advertiser_id}/transactions"
VALID_MERCHANT_TYPES = ["", "BRAND", "SELLER"]


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
# Feed Sync helpers
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


def _feed_post(session, endpoint, payload, headers, result, label, retry_attempts=RETRY_ATTEMPTS):
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


def run_v2_sync(retailer_id, token, products, progress_bar, status_text):
    result = SyncResult(label="Feed V2", total=len(products))
    headers = {"x-retailer-id": retailer_id, "x-token": token, "Content-Type": "application/json"}
    session = requests.Session()
    min_interval = 1.0 / RATE_LIMIT
    batches = [products[i:i + BATCH_SIZE] for i in range(0, len(products), BATCH_SIZE)]
    for idx, batch in enumerate(batches):
        if idx > 0:
            time.sleep(min_interval)
        ok = _feed_post(session, API_FEED_V2, {"products": batch}, headers, result, f"Batch {idx+1}")
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
        ok = _feed_post(session, API_MULTI_LANG, payload, headers, result, f"Batch {idx+1}")
        if ok:
            result.synced += len(batch)
        else:
            result.failed += len(batch)
        progress_bar.progress((idx + 1) / len(batches), text=f"Batch {idx+1}/{len(batches)}")
        status_text.text(f"Synced {result.synced}/{result.total} products")
    return result


# ---------------------------------------------------------------------------
# Advertiser & Wallet helpers
# ---------------------------------------------------------------------------

def _api_request(session, method, url, headers, logs, label, payload=None, params=None,
                 success_codes=(200, 201)):
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            resp = session.request(method, url, json=payload, params=params,
                                   headers=headers, timeout=30)
            if resp.status_code in success_codes:
                data = resp.json()
                logs.append(f"[{label}] OK  response={json.dumps(data)[:300]}")
                return True, data
            try:
                error = resp.json().get("error", {})
                logs.append(f"[{label}] FAIL attempt={attempt}/{RETRY_ATTEMPTS} "
                            f"http={resp.status_code} code={error.get('code')} "
                            f"msg={error.get('message')}")
            except Exception:
                logs.append(f"[{label}] FAIL attempt={attempt}/{RETRY_ATTEMPTS} "
                            f"http={resp.status_code} body={resp.text[:300]}")
        except requests.RequestException as exc:
            logs.append(f"[{label}] ERROR attempt={attempt}/{RETRY_ATTEMPTS} {exc}")
        if attempt < RETRY_ATTEMPTS:
            time.sleep(RETRY_DELAY)
    return False, None


def _adv_post(session, payload, headers, logs, label):
    ok, _ = _api_request(session, "POST", API_ADVERTISER_CREATE, headers, logs, label, payload=payload)
    return ok


def build_adv_payload(name, merchant_id, alias=None, merchant_type=None):
    payload = {"name": name, "merchant_id": merchant_id}
    if alias:
        payload["alias"] = alias
    if merchant_type:
        payload["merchant_type"] = merchant_type
    return payload


def validate_adv_rows(rows):
    valid, skipped = [], []
    for i, row in enumerate(rows, start=2):
        name = (row.get("name") or "").strip()
        merchant_id = (row.get("merchant_id") or "").strip()
        if not name or not merchant_id:
            skipped.append({"row": i, "reason": f"missing {'name' if not name else 'merchant_id'}", "data": row})
            continue
        merchant_type = (row.get("merchant_type") or "").strip().upper() or None
        if merchant_type and merchant_type not in ("BRAND", "SELLER"):
            merchant_type = None
        valid.append(build_adv_payload(
            name=name, merchant_id=merchant_id,
            alias=(row.get("alias") or "").strip() or None,
            merchant_type=merchant_type,
        ))
    return valid, skipped


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------

def go_to(page_name):
    st.session_state["page"] = page_name


if "page" not in st.session_state:
    st.session_state["page"] = "feed_home"


# =====================================================================
# PAGE CONFIG + LEFT SIDEBAR
# =====================================================================

st.set_page_config(page_title="Osmos Operations Hub", page_icon="🔄", layout="wide")

with st.sidebar:
    st.header("🔄 Osmos Operations Hub")
    st.divider()

    # --- Tool selector ---
    st.subheader("Tools")
    current = st.session_state["page"]

    is_feed = current.startswith("feed_") or current in ("single", "multi_step1", "multi_step2", "multi_step3")
    is_adv = current.startswith("adv_")

    if st.button("📦  Feed Sync", key="nav_feed", use_container_width=True,
                 type="primary" if is_feed else "secondary"):
        go_to("feed_home")
        st.rerun()

    if st.button("🏢  Advertiser & Wallet", key="nav_adv", use_container_width=True,
                 type="primary" if is_adv else "secondary"):
        go_to("adv_main")
        st.rerun()

    # --- Feed sub-nav ---
    if is_feed and current != "feed_home":
        st.divider()
        st.caption("Feed Sync")
        is_single = current == "single"
        is_multi = current in ("multi_step1", "multi_step2", "multi_step3")

        if st.button("Single Language", key="nav_single", use_container_width=True,
                     type="primary" if is_single else "secondary"):
            go_to("single")
            st.rerun()
        if st.button("Multi-Language", key="nav_multi", use_container_width=True,
                     type="primary" if is_multi else "secondary"):
            go_to("multi_step1")
            st.rerun()

    st.divider()

    # --- API Credentials ---
    st.subheader("API Credentials")
    retailer_id = st.text_input("Retailer ID (x-retailer-id)", type="default",
                                help="Agency ID from Osmos developer settings")
    token = st.text_input("API Token (x-token)", type="password",
                          help="Token from Osmos developer settings")
    st.divider()
    st.caption(f"Batch: {BATCH_SIZE} | Rate: {RATE_LIMIT} req/s | Retries: {RETRY_ATTEMPTS}")

has_creds = bool(retailer_id and token)
remap = DEFAULT_REMAP
current = st.session_state["page"]


def make_headers():
    return {"x-retailer-id": retailer_id, "x-token": token, "Content-Type": "application/json"}


# #####################################################################
#                        FEED SYNC PAGES
# #####################################################################

# =====================================================================
# FEED HOME — Choose sync type
# =====================================================================
if current == "feed_home":
    st.title("Feed Sync")
    st.markdown("####")
    st.subheader("What kind of feed sync do you want?")
    st.markdown("")

    col1, col2 = st.columns(2, gap="large")

    with col1:
        st.markdown(
            """
            ### Single Language
            Sync one feed with **all fields** via the Feed V2 API.

            - Upload a single CSV/TSV
            - All columns sent as-is
            - No language tagging
            """
        )
        if st.button("Single Language →", key="go_single", type="primary", use_container_width=True):
            go_to("single")
            st.rerun()

    with col2:
        st.markdown(
            """
            ### Multi-Language
            Sync the full feed **+ translated feeds** with language codes.

            - Step 1: Feed V2 (all fields)
            - Step 2: Multi-Language (text fields + ISO code)
            - Step 3: cURL Generator
            """
        )
        if st.button("Multi-Language →", key="go_multi", type="primary", use_container_width=True):
            go_to("multi_step1")
            st.rerun()


# =====================================================================
# SINGLE LANGUAGE — V2 sync only
# =====================================================================
elif current == "single":
    st.title("Single Language — Feed Sync V2")
    st.caption("Upload the product feed. All columns are sent as-is to the Feed V2 API.")

    v2_file = st.file_uploader("Upload feed file (CSV / TSV)", type=["csv", "tsv"], key="single_file")

    if v2_file:
        fieldnames, rows = parse_csv(v2_file)
        products, skipped = extract_products(rows, remap)
        st.session_state["v2_products"] = products

        c1, c2, c3 = st.columns(3)
        c1.metric("Products", f"{len(products):,}")
        c2.metric("Fields", len(fieldnames))
        c3.metric("Skipped", skipped)

        with st.expander("Preview payload — first product"):
            if products:
                st.json(products[0])

        with st.expander("cURL for single SKU"):
            if products:
                sku_ids = [p["id"] for p in products]
                sel = st.selectbox("Select SKU", sku_ids, key="single_sku")
                prod = next(p for p in products if p["id"] == sel)
                st.code(_make_curl(API_FEED_V2, retailer_id, token, {"products": [prod]}), language="bash")

        st.markdown("---")

        col1, col2 = st.columns(2)
        with col1:
            if st.button("Dry Run", key="s_dry", use_container_width=True):
                n = (len(products) + BATCH_SIZE - 1) // BATCH_SIZE
                st.info(f"**Dry Run:** {len(products):,} products in {n} batches")
                st.code(json.dumps({"products": [products[0]]}, ensure_ascii=False, indent=2), language="json")
        with col2:
            do_sync = st.button("Sync Feed V2", key="s_sync", type="primary",
                                use_container_width=True, disabled=not has_creds)

        if not has_creds:
            st.caption("Enter API credentials in the sidebar to enable sync.")

        if do_sync and products:
            progress = st.progress(0, text="Starting...")
            status = st.empty()
            result = run_v2_sync(retailer_id, token, products, progress, status)
            if result.failed == 0:
                st.success(f"**{result.synced:,}/{result.total:,}** products synced ✅")
                play_success()
            else:
                st.error(f"{result.synced:,} synced, {result.failed:,} failed ❌")
            with st.expander("Sync logs"):
                for line in result.logs:
                    st.text(line)


# =====================================================================
# MULTI-LANGUAGE — Step 1: Feed V2
# =====================================================================
elif current == "multi_step1":
    st.caption("Step 1 of 3")
    st.progress(1 / 3)
    st.title("Step 1 — Feed Sync V2")
    st.caption("Upload the **full product feed**. All columns are sent as-is — nothing is filtered.")

    v2_file = st.file_uploader("Upload feed file (CSV / TSV)", type=["csv", "tsv"], key="m_v2_file")

    if v2_file:
        fieldnames, rows = parse_csv(v2_file)
        products, skipped = extract_products(rows, remap)
        st.session_state["v2_products"] = products
        st.session_state["v2_filename"] = v2_file.name

        c1, c2, c3 = st.columns(3)
        c1.metric("Products", f"{len(products):,}")
        c2.metric("Fields", len(fieldnames))
        c3.metric("Skipped", skipped)

        with st.expander("Preview payload — first product"):
            if products:
                st.json(products[0])

        st.markdown("---")

        col1, col2 = st.columns(2)
        with col1:
            if st.button("Dry Run", key="m1_dry", use_container_width=True):
                n = (len(products) + BATCH_SIZE - 1) // BATCH_SIZE
                st.info(f"**Dry Run:** {len(products):,} products in {n} batches")
                st.code(json.dumps({"products": [products[0]]}, ensure_ascii=False, indent=2), language="json")
        with col2:
            do_sync = st.button("Sync Feed V2", key="m1_sync", type="primary",
                                use_container_width=True, disabled=not has_creds)

        if not has_creds:
            st.caption("Enter API credentials in the sidebar to enable sync.")

        if do_sync and products:
            progress = st.progress(0, text="Starting...")
            status = st.empty()
            result = run_v2_sync(retailer_id, token, products, progress, status)
            if result.failed == 0:
                st.success(f"**{result.synced:,}/{result.total:,}** products synced ✅")
                st.session_state["v2_sync_done"] = True
                play_success()
            else:
                st.error(f"{result.synced:,} synced, {result.failed:,} failed ❌")
            with st.expander("Sync logs"):
                for line in result.logs:
                    st.text(line)

        st.markdown("---")
        if st.session_state.get("v2_sync_done"):
            if st.button("Next → Multi-Language Sync", type="primary", use_container_width=True):
                go_to("multi_step2")
                st.rerun()
        else:
            st.info("Complete Feed V2 sync successfully to proceed to the next step.")


# =====================================================================
# MULTI-LANGUAGE — Step 2: ML Sync
# =====================================================================
elif current == "multi_step2":
    st.caption("Step 2 of 3")
    st.progress(2 / 3)
    st.title("Step 2 — Multi-Language Sync")
    st.caption("Upload the **translated feed**. Only text fields are sent with the language code.")

    ml_file = st.file_uploader("Upload translated feed file (CSV / TSV)", type=["csv", "tsv"], key="m_ml_file")

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

    ml_text_fields = sorted(ML_FIELDS - {"id"})
    st.caption(f"**Fields sent:** `{'`, `'.join(ml_text_fields)}`")

    if ml_file:
        fieldnames, rows = parse_csv(ml_file)
        products_raw, skipped = extract_products(rows, remap)
        products_clean = [ml_only(p) for p in products_raw]
        st.session_state["ml_products_raw"] = products_raw
        st.session_state["ml_products_clean"] = products_clean
        st.session_state["ml_lang_val"] = ml_lang

        c1, c2, c3 = st.columns(3)
        c1.metric("Products", f"{len(products_clean):,}")
        c2.metric("ML Fields", len(products_clean[0]) if products_clean else 0)
        c3.metric("Skipped", skipped)

        with st.expander("Preview ML payload — first product"):
            if products_clean:
                st.json({"language": ml_lang, "products": [products_clean[0]]})

        st.markdown("---")

        col1, col2 = st.columns(2)
        with col1:
            if st.button("Dry Run", key="m2_dry", use_container_width=True):
                n = (len(products_raw) + BATCH_SIZE - 1) // BATCH_SIZE
                st.info(f"**Dry Run:** {len(products_raw):,} products in {n} batches (language=`{ml_lang}`)")
                st.code(json.dumps({"language": ml_lang, "products": [products_clean[0]]},
                                   ensure_ascii=False, indent=2), language="json")
        with col2:
            do_sync = st.button("Sync Multi-Language", key="m2_sync", type="primary",
                                use_container_width=True, disabled=not has_creds)

        if not has_creds:
            st.caption("Enter API credentials in the sidebar to enable sync.")

        if do_sync and products_raw:
            progress = st.progress(0, text=f"Starting ML sync (language={ml_lang})...")
            status = st.empty()
            result = run_ml_sync(retailer_id, token, ml_lang, products_raw, progress, status)
            if result.failed == 0:
                st.success(f"**{result.synced:,}/{result.total:,}** products synced ✅  (language=`{ml_lang}`)")
                st.session_state["ml_sync_done"] = True
                play_success()
            else:
                st.error(f"{result.synced:,} synced, {result.failed:,} failed ❌")
            with st.expander("Sync logs"):
                for line in result.logs:
                    st.text(line)

    st.markdown("---")
    c_back, c_next = st.columns(2)
    with c_back:
        if st.button("← Back to Feed V2", use_container_width=True):
            go_to("multi_step1")
            st.rerun()
    with c_next:
        if st.session_state.get("ml_sync_done"):
            if st.button("Next → cURL Generator", type="primary", use_container_width=True):
                go_to("multi_step3")
                st.rerun()
        else:
            st.info("Complete ML sync successfully to proceed.")


# =====================================================================
# MULTI-LANGUAGE — Step 3: cURL Generator
# =====================================================================
elif current == "multi_step3":
    st.caption("Step 3 of 3")
    st.progress(3 / 3)
    st.title("Step 3 — cURL Generator")
    st.caption("Generate copy-pasteable cURL commands for any single SKU.")

    v2_products = st.session_state.get("v2_products", [])
    ml_products_raw = st.session_state.get("ml_products_raw", [])
    ml_products_clean = st.session_state.get("ml_products_clean", [])
    ml_lang = st.session_state.get("ml_lang_val", "ar")

    if not v2_products and not ml_products_clean:
        st.info("Upload feeds in Step 1 and Step 2 first.")
    else:
        v2_by_id = {p["id"]: p for p in v2_products}
        ml_by_id = {p["id"]: p for p in ml_products_clean}
        all_ids = sorted(set(v2_by_id) | set(ml_by_id))

        selected_sku = st.selectbox("Select SKU (product id)", all_ids, key="curl_sku")

        if selected_sku:
            has_v2 = selected_sku in v2_by_id
            has_ml = selected_sku in ml_by_id

            if has_v2:
                st.markdown("### Feed V2 cURL")
                st.code(_make_curl(API_FEED_V2, retailer_id, token,
                                   {"products": [v2_by_id[selected_sku]]}), language="bash")

            if has_ml:
                st.markdown(f"### Multi-Language cURL (language=`{ml_lang}`)")
                st.code(_make_curl(API_MULTI_LANG, retailer_id, token,
                                   {"language": ml_lang, "products": [ml_by_id[selected_sku]]}),
                        language="bash")

    st.markdown("---")
    if st.button("← Back to Multi-Language Sync", use_container_width=True):
        go_to("multi_step2")
        st.rerun()


# #####################################################################
#                   ADVERTISER & WALLET PAGE
# #####################################################################

elif current == "adv_main":
    st.title("Advertiser & Wallet Management")
    st.caption("Create advertisers, look up IDs, manage wallets, and create transactions.")

    tab_single, tab_wallet = st.tabs(["Single Advertiser", "Wallet"])

    # ── Tab 1: Single Advertiser ─────────────────────────────────────────
    with tab_single:
        adv_action = st.radio("Action", ["Lookup by Merchant ID", "Create Advertiser"],
                              horizontal=True, label_visibility="collapsed", key="adv_action")
        st.divider()

        # ── Lookup ───────────────────────────────────────────────────────
        if adv_action == "Lookup by Merchant ID":
            st.markdown("#### Lookup Advertiser — get your Advertiser ID")
            st.caption("Use this to find the `advertiser_id` needed for all Wallet operations.")

            lu_merchant_id = st.text_input("Merchant ID *", key="lu_mid", placeholder="acme_001",
                                           help="The unique ID given by the retailer to the merchant/brand/seller")

            lookup_btn = st.button("Lookup Advertiser", type="primary", key="lookup_btn",
                                   disabled=not (has_creds and lu_merchant_id))
            if not has_creds:
                st.caption("Enter API credentials in the sidebar.")

            if lookup_btn:
                logs = []
                session = requests.Session()
                with st.spinner("Looking up advertiser..."):
                    ok, data = _api_request(session, "GET", API_ADVERTISER_GET, make_headers(), logs,
                                            f"lookup {lu_merchant_id}",
                                            params={"merchant_id": lu_merchant_id})
                if ok and data:
                    adv_id = (data.get("advertiser_id") or data.get("id") or
                              (data.get("data") or {}).get("advertiser_id") or "")
                    adv_name = data.get("name") or data.get("advertiser_name") or ""
                    adv_status = data.get("status") or ""

                    st.success("Advertiser found.")
                    c1, c2, c3 = st.columns(3)
                    if adv_id:
                        c1.metric("Advertiser ID", adv_id)
                    if adv_name:
                        c2.metric("Name", adv_name)
                    if adv_status:
                        c3.metric("Status", adv_status)
                    st.json(data)

                    if adv_id:
                        st.session_state["lw_last_advertiser_id"] = adv_id
                        st.info(f"Advertiser ID `{adv_id}` saved — switch to the **Wallet** tab to list wallets.")
                else:
                    st.error("Advertiser not found. See logs below.")
                with st.expander("Logs"):
                    for line in logs:
                        st.text(line)

        # ── Create ───────────────────────────────────────────────────────
        else:
            st.markdown("#### Create a Single Advertiser")

            col1, col2 = st.columns(2)
            with col1:
                s_name = st.text_input("Name *", placeholder="Acme Corp")
                s_merchant_id = st.text_input("Merchant ID *", placeholder="acme_001")
            with col2:
                s_alias = st.text_input("Alias", placeholder="acme (optional)")
                s_merchant_type = st.selectbox("Merchant Type", options=VALID_MERCHANT_TYPES,
                                               format_func=lambda x: x if x else "— not set —")

            if s_name and s_merchant_id:
                payload_preview = build_adv_payload(
                    name=s_name, merchant_id=s_merchant_id,
                    alias=s_alias or None, merchant_type=s_merchant_type or None,
                )
                with st.expander("Payload preview", expanded=False):
                    st.json(payload_preview)

            col_dry, col_create = st.columns(2)
            with col_dry:
                single_dry = st.button("Dry Run", key="single_dry", use_container_width=True)
            with col_create:
                single_create = st.button("Create Advertiser", type="primary", key="single_create",
                                          use_container_width=True,
                                          disabled=not (has_creds and s_name and s_merchant_id))

            if not has_creds:
                st.caption("Enter API credentials in the sidebar to enable creation.")

            if single_dry:
                if not s_name or not s_merchant_id:
                    st.error("Name and Merchant ID are required.")
                else:
                    payload = build_adv_payload(s_name, s_merchant_id, s_alias or None, s_merchant_type or None)
                    st.success("Dry run — payload is valid.")
                    st.json(payload)

            if single_create:
                payload = build_adv_payload(s_name, s_merchant_id, s_alias or None, s_merchant_type or None)
                logs = []
                session = requests.Session()
                with st.spinner("Creating advertiser..."):
                    ok = _adv_post(session, payload, make_headers(), logs, s_name)
                if ok:
                    st.success(f"Advertiser **{s_name}** created successfully.")
                    play_success()
                else:
                    st.error(f"Failed to create **{s_name}**. See logs below.")
                with st.expander("Logs"):
                    for line in logs:
                        st.text(line)

    # ── Tab 2: Wallet ────────────────────────────────────────────────────
    with tab_wallet:
        st.subheader("Wallet Management")

        w_section = st.radio("Action", ["List Wallets", "Get Balance", "Create Transaction"],
                             horizontal=True, label_visibility="collapsed")
        st.divider()

        # ── List Wallets ─────────────────────────────────────────────────
        if w_section == "List Wallets":
            st.markdown("#### List Wallets — find your Wallet ID")
            col1, col2, col3 = st.columns([2, 1, 1])
            with col1:
                lw_advertiser_id = st.text_input("Advertiser ID *", key="lw_adv", placeholder="adv_001")
            with col2:
                lw_limit = st.number_input("Limit", min_value=1, max_value=100, value=10, key="lw_limit")
            with col3:
                lw_offset = st.number_input("Offset", min_value=0, value=0, key="lw_offset")
            lw_overall = st.checkbox("Include overall balance", key="lw_overall")

            list_btn = st.button("List Wallets", type="primary", key="list_wallets",
                                 disabled=not (has_creds and lw_advertiser_id))
            if not has_creds:
                st.caption("Enter API credentials in the sidebar.")

            if list_btn:
                url = API_WALLET_BASE.format(advertiser_id=lw_advertiser_id)
                params = {"limit": lw_limit, "offset": lw_offset}
                if lw_overall:
                    params["overall_required"] = "true"
                logs = []
                session = requests.Session()
                with st.spinner("Fetching wallets..."):
                    ok, data = _api_request(session, "GET", url, make_headers(), logs,
                                            "list wallets", params=params)
                if ok and data:
                    wallets = data if isinstance(data, list) else data.get("wallets") or data.get("data") or [data]
                    st.success(f"Found {len(wallets)} wallet(s).")
                    if wallets:
                        display_keys = ["wallet_id", "id", "wallet_name", "name", "currency",
                                        "balance", "current_balance", "payment_type", "status"]
                        rows_display = []
                        for w in wallets:
                            row = {k: w.get(k, "") for k in display_keys if k in w}
                            rows_display.append(row)
                        if rows_display:
                            st.dataframe(rows_display, use_container_width=True)
                        else:
                            st.json(wallets)

                        st.session_state["lw_last_advertiser_id"] = lw_advertiser_id
                        first_id = (wallets[0].get("wallet_id") or wallets[0].get("id") or "") if wallets else ""
                        st.session_state["lw_last_wallet_id"] = first_id
                        if first_id:
                            st.info("Tip: switch to **Get Balance** or **Create Transaction** — IDs will be pre-filled.")
                    else:
                        st.warning("No wallets returned.")
                else:
                    st.error("Failed to list wallets. See logs below.")
                with st.expander("Logs"):
                    for line in logs:
                        st.text(line)

        # ── Get Balance ──────────────────────────────────────────────────
        elif w_section == "Get Balance":
            st.markdown("#### Get Wallet Balance")
            _prefill_adv = st.session_state.get("lw_last_advertiser_id", "")
            _prefill_wlt = st.session_state.get("lw_last_wallet_id", "")
            col1, col2 = st.columns(2)
            with col1:
                gb_advertiser_id = st.text_input("Advertiser ID *", key="gb_adv",
                                                 placeholder="adv_001", value=_prefill_adv)
            with col2:
                gb_wallet_id = st.text_input("Wallet ID *", key="gb_wid",
                                             placeholder="wlt_001", value=_prefill_wlt)
            gb_overall = st.checkbox("Include overall balance", key="gb_overall")

            get_btn = st.button("Get Balance", type="primary", key="get_balance",
                                disabled=not (has_creds and gb_advertiser_id and gb_wallet_id))
            if not has_creds:
                st.caption("Enter API credentials in the sidebar.")

            if get_btn:
                url = API_WALLET_BY_ID.format(advertiser_id=gb_advertiser_id, wallet_id=gb_wallet_id)
                params = {"overall_required": "true"} if gb_overall else {}
                logs = []
                session = requests.Session()
                with st.spinner("Fetching wallet..."):
                    ok, data = _api_request(session, "GET", url, make_headers(), logs,
                                            f"wallet {gb_wallet_id}", params=params)
                if ok and data:
                    st.success("Wallet found.")
                    balance = data.get("balance") or data.get("current_balance")
                    currency = data.get("currency")
                    wallet_name = data.get("wallet_name") or data.get("name")
                    if balance is not None or currency:
                        cols = st.columns(3)
                        if wallet_name:
                            cols[0].metric("Wallet Name", wallet_name)
                        if balance is not None:
                            cols[1].metric("Balance", f"{balance} {currency or ''}".strip())
                        if currency:
                            cols[2].metric("Currency", currency)
                    st.json(data)
                else:
                    st.error("Failed to fetch wallet. See logs below.")
                with st.expander("Logs"):
                    for line in logs:
                        st.text(line)

        # ── Create Transaction ───────────────────────────────────────────
        else:
            st.markdown("#### Create Transaction")
            _prefill_adv_t = st.session_state.get("lw_last_advertiser_id", "")
            _prefill_wlt_t = st.session_state.get("lw_last_wallet_id", "")
            if _prefill_adv_t or _prefill_wlt_t:
                st.info(f"Pre-filled from List Wallets — Advertiser: `{_prefill_adv_t}` · Wallet: `{_prefill_wlt_t}`")

            col1, col2 = st.columns(2)
            with col1:
                ct_advertiser_id = st.text_input("Advertiser ID *", key="ct_adv",
                                                 placeholder="adv_001", value=_prefill_adv_t)
                ct_partner_tx_id = st.text_input("Partner Transaction ID *", key="ct_ptxid",
                                                 placeholder="TXN-2024-001",
                                                 help="Your marketplace-side transaction ID (max 128 chars).")
                ct_amount = st.number_input("Amount *", min_value=0.01, value=100.00,
                                            step=0.01, format="%.2f", key="ct_amount",
                                            help="Transaction amount (must be > 0)")
                ct_currency = st.text_input("Currency *", key="ct_currency", placeholder="USD",
                                            max_chars=3, help="3-character retailer currency code")
            with col2:
                ct_wallet_id = st.text_input("Wallet ID", key="ct_wid",
                                             placeholder="wlt_001 (optional)", value=_prefill_wlt_t)
                ct_credit_type = st.selectbox("Credit Type", options=["PREPAID", "INCENTIVE"],
                                              key="ct_credit_type",
                                              help="Defaults to PREPAID if not set")
                ct_description = st.text_area("Description", key="ct_desc", placeholder="Optional")

            _tx_ready = ct_advertiser_id and ct_partner_tx_id and ct_amount and ct_currency
            if _tx_ready:
                tx_preview = {
                    "partner_transaction_id": ct_partner_tx_id,
                    "amount": ct_amount,
                    "currency": ct_currency.upper(),
                    "credit_type": ct_credit_type,
                }
                if ct_wallet_id:
                    tx_preview["wallet_id"] = ct_wallet_id
                if ct_description:
                    tx_preview["description"] = ct_description
                with st.expander("Payload preview"):
                    st.json(tx_preview)

            create_tx_btn = st.button(
                "Create Transaction", type="primary", key="create_tx",
                disabled=not (has_creds and bool(_tx_ready))
            )
            if not has_creds:
                st.caption("Enter API credentials in the sidebar.")

            if create_tx_btn:
                url = API_TRANSACTION_CREATE.format(advertiser_id=ct_advertiser_id)
                body = {
                    "partner_transaction_id": ct_partner_tx_id,
                    "amount": ct_amount,
                    "currency": ct_currency.upper(),
                    "credit_type": ct_credit_type,
                }
                if ct_wallet_id:
                    body["wallet_id"] = ct_wallet_id
                if ct_description:
                    body["description"] = ct_description
                logs = []
                session = requests.Session()
                with st.spinner("Creating transaction..."):
                    ok, data = _api_request(session, "POST", url, make_headers(), logs,
                                            f"transaction {ct_partner_tx_id}", payload=body,
                                            success_codes=(200, 201))
                if ok:
                    st.success(f"Transaction **{ct_partner_tx_id}** created successfully.")
                    play_success()
                    if data:
                        tx_id = data.get("transaction_id") or data.get("id") or ""
                        if tx_id:
                            st.metric("Transaction ID", tx_id)
                        st.json(data)
                else:
                    st.error("Failed to create transaction. See logs below.")
                with st.expander("Logs"):
                    for line in logs:
                        st.text(line)
