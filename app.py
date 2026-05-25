"""
Osmos Feed Sync — Streamlit Wizard UI

Screen 1 — Choose sync type: Single Language or Multi-Language
  Single Language → V2 sync only (all fields, one step)
  Multi-Language  → Step 1: V2 sync (full feed)
                    Step 2: ML sync (translated feed + language code)
                    Step 3: cURL generator

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
    """Load success_sound.mp3 and return base64-encoded string."""
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


def play_powerup():
    """Play custom success sound for intermediate sync steps."""
    st_html(_play_audio_js(_load_success_sound_b64()), height=0)


def play_stage_clear():
    """Play custom success sound for final multi-language sync."""
    st_html(_play_audio_js(_load_success_sound_b64()), height=0)


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
# Navigation helpers
# ---------------------------------------------------------------------------

def go_to(page_name):
    st.session_state["page"] = page_name

if "page" not in st.session_state:
    st.session_state["page"] = "home"


# ---------------------------------------------------------------------------
# Page config + sidebar
# ---------------------------------------------------------------------------

st.set_page_config(page_title="Osmos Feed Sync", page_icon="🔄", layout="wide")

with st.sidebar:
    st.header("API Credentials")
    retailer_id = st.text_input("Retailer ID (x-retailer-id)", type="default",
                                help="Agency ID from Osmos developer settings")
    token = st.text_input("API Token (x-token)", type="password",
                          help="Token from Osmos developer settings")
    st.divider()
    st.caption(f"Batch: {BATCH_SIZE} | Rate: {RATE_LIMIT} req/s | Retries: {RETRY_ATTEMPTS}")

    # Quick nav back to home
    st.divider()
    if st.button("← Start Over", use_container_width=True):
        go_to("home")
        st.rerun()

has_creds = bool(retailer_id and token)
remap = DEFAULT_REMAP
current = st.session_state["page"]


# =====================================================================
# HOME — Choose sync type
# =====================================================================
if current == "home":
    st.title("Osmos Feed Sync")
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
                play_powerup()
            else:
                st.error(f"{result.synced:,} synced, {result.failed:,} failed ❌")
            with st.expander("Sync logs"):
                for line in result.logs:
                    st.text(line)


# =====================================================================
# MULTI-LANGUAGE — Step 1: Feed V2
# =====================================================================
elif current == "multi_step1":
    # Step indicator
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
                play_powerup()
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
                play_stage_clear()
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
