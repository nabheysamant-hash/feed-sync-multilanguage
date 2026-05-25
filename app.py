"""
Osmos Multi-Language Feed Sync — Streamlit UI

Each file is synced in two calls per batch:
  1. Feed Sync V2  (/products)           — all fields (price, image, availability, etc.)
  2. Multi-Language (/products/multiLanguage) — translatable text fields + language code

Both happen automatically in a single "Sync" click.

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

# --- Multi-Language API accepted fields ---
ML_FIELDS = {"id", "title", "description", "brand", "category",
             "custom_label_0", "custom_label_1", "custom_label_2",
             "secondary_categories"}

REQUIRED_FIELDS = {"id", "title", "description", "brand"}

# CSV column → API field remapping
DEFAULT_REMAP = {
    "image_url": "image_link",
}

LANGUAGE_OPTIONS = {
    "en": "English",
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
}

_STRIP_CHARS = str.maketrans("", "", "\r\n\0\t")

# Languages that should SKIP Multi-Language API (base/default language)
BASE_LANGUAGES = {"en"}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SyncResult:
    language: str
    filename: str
    total: int = 0
    v2_synced: int = 0
    v2_failed: int = 0
    ml_synced: int = 0
    ml_failed: int = 0
    skipped: int = 0
    logs: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core logic
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
    """All columns included. Returns (products, skipped)."""
    products = []
    skipped = 0
    for row in rows:
        mapped = remap_row(row, remap)
        missing = REQUIRED_FIELDS - mapped.keys()
        if missing:
            skipped += 1
            continue
        products.append(mapped)
    return products, skipped


def _clean_text(value: str) -> str:
    """Remove Unicode control chars, zero-width chars, and normalise whitespace."""
    # Strip Unicode control characters (Cc category) except normal space
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Cc")
    # Remove zero-width and bidi control chars
    value = re.sub(r"[​-‏‪-‮⁦-⁩﻿؜]", "", value)
    # Collapse multiple spaces into one
    value = re.sub(r" {2,}", " ", value).strip()
    return value


def ml_only(product: dict) -> dict:
    """Filter product to only Multi-Language accepted fields + clean text."""
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
                result.logs.append(
                    f"[{label}] OK  request_id={data.get('request_id', '?')}")
                return True
            # Log full response for debugging
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


def run_sync(retailer_id, token, language, products, filename,
             progress_bar, status_text, skip_ml=False):
    """
    Two-phase sync:
      Phase 1 — Feed V2 (all fields) for every batch
      Phase 2 — Multi-Language (text fields + language) for every batch
    Separating the phases avoids interleaving that caused 500 errors.
    """
    result = SyncResult(language=language, filename=filename, total=len(products))
    headers = {
        "x-retailer-id": retailer_id,
        "x-token": token,
        "Content-Type": "application/json",
    }
    session = requests.Session()
    min_interval = 1.0 / RATE_LIMIT

    batches = [products[i:i + BATCH_SIZE] for i in range(0, len(products), BATCH_SIZE)]
    total_batches = len(batches)
    total_steps = total_batches * (1 if skip_ml else 2)
    step = 0

    # ── Phase 1: Feed V2 (all fields) ──
    result.logs.append("── Phase 1: Feed V2 (all fields) ──")
    for idx, batch in enumerate(batches):
        if idx > 0:
            time.sleep(min_interval)

        v2_payload = {"products": batch}
        v2_ok = _post(session, API_FEED_V2, v2_payload, headers, result, f"V2 batch {idx+1}")

        if v2_ok:
            result.v2_synced += len(batch)
        else:
            result.v2_failed += len(batch)

        step += 1
        progress_bar.progress(step / total_steps, text=f"V2 batch {idx+1}/{total_batches}")
        status_text.text(f"V2: {result.v2_synced}/{result.total} synced")

    if skip_ml:
        result.logs.append("── ML sync skipped (base language) ──")
        return result

    # ── Pause between phases ──
    result.logs.append("── Pausing 3s before ML phase ──")
    time.sleep(3)

    # ── Phase 2: Multi-Language (text fields + language) ──
    result.logs.append(f"── Phase 2: Multi-Language (language={language}) ──")
    for idx, batch in enumerate(batches):
        if idx > 0:
            time.sleep(max(min_interval, 1.0))   # 1s gap between ML batches

        ml_batch = [ml_only(p) for p in batch]
        ml_payload = {"language": language, "products": ml_batch}
        result.logs.append(f"[ML batch {idx+1}] Sending {len(ml_batch)} products, "
                           f"fields={sorted(ml_batch[0].keys()) if ml_batch else '?'}")
        ml_ok = _post(session, API_MULTI_LANG, ml_payload, headers, result, f"ML batch {idx+1}")

        if ml_ok:
            result.ml_synced += len(batch)
        else:
            result.ml_failed += len(batch)

        step += 1
        progress_bar.progress(step / total_steps, text=f"ML batch {idx+1}/{total_batches}")
        status_text.text(f"V2: {result.v2_synced}/{result.total} | ML: {result.ml_synced}/{result.total}")

    return result


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------

st.set_page_config(page_title="Osmos Feed Sync", page_icon="🔄", layout="wide")

st.title("Osmos Multi-Language Feed Sync")
st.caption("Upload per-language feed files → syncs all fields (V2) + language tag (Multi-Language) in one click")

# --- Sidebar ---
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

    st.divider()
    st.header("How it works")
    st.caption(
        "**Two-phase sync per file:**\n\n"
        "1. **Phase 1 — Feed V2** → all fields (price, image, availability…)\n"
        "2. **Phase 2 — Multi-Language** → text fields (title, description, brand, category) + language code\n\n"
        "Phases run sequentially to avoid server conflicts."
    )

# --- File upload ---
st.subheader("Upload Feed Files")
st.info(
    "Upload one CSV/TSV per language. All columns are synced via Feed V2. "
    "Translated text fields are also synced via the Multi-Language API with the language code."
)

uploaded_files = st.file_uploader(
    "Drop your feed files here",
    type=["csv", "tsv"],
    accept_multiple_files=True,
)

if not uploaded_files:
    st.stop()

# --- Per-file config ---
st.subheader("Configure Languages")

file_configs = []
cols = st.columns(min(len(uploaded_files), 3))

for i, uf in enumerate(uploaded_files):
    col = cols[i % len(cols)]
    with col:
        st.markdown(f"**{uf.name}**")
        fieldnames, rows = parse_csv(uf)
        st.caption(f"{len(rows):,} rows · {len(fieldnames)} columns")

        # auto-detect language
        default_lang = "en"
        name_lower = uf.name.lower()
        if "ar" in name_lower:
            default_lang = "ar"
        elif "fr" in name_lower:
            default_lang = "fr"
        elif "de" in name_lower:
            default_lang = "de"
        elif "es" in name_lower:
            default_lang = "es"
        elif "tr" in name_lower:
            default_lang = "tr"

        lang_keys = list(LANGUAGE_OPTIONS.keys())
        default_idx = lang_keys.index(default_lang) if default_lang in lang_keys else 0

        lang = st.selectbox(
            "Language",
            options=lang_keys,
            format_func=lambda k: LANGUAGE_OPTIONS[k],
            index=default_idx,
            key=f"lang_{i}",
        )

        with st.expander("Field details", expanded=False):
            remapped = [remap.get(fn, fn) for fn in fieldnames]
            v2_fields = remapped
            ml_fields_present = [n for n in remapped if n in ML_FIELDS]
            st.markdown(f"**Feed V2** sends all {len(v2_fields)} fields")
            st.markdown(f"**Multi-Language** sends: `{'`, `'.join(ml_fields_present)}`")

        file_configs.append({
            "file": uf,
            "filename": uf.name,
            "language": lang,
            "rows": rows,
            "fieldnames": fieldnames,
        })

# --- Preview ---
st.subheader("Preview")

for cfg in file_configs:
    products, skipped = extract_products(cfg["rows"], remap)
    lang_label = LANGUAGE_OPTIONS.get(cfg["language"], cfg["language"])
    with st.expander(
        f"{cfg['filename']} — {lang_label} | {len(products):,} products · {skipped} skipped"
    ):
        if products:
            tab_v2, tab_ml = st.tabs(["Feed V2 payload (all fields)", "Multi-Language payload (text fields)"])
            with tab_v2:
                st.json(products[0])
            with tab_ml:
                st.json(ml_only(products[0]))
        else:
            st.warning("No valid products.")

# --- cURL Generator ---
st.subheader("cURL for Single SKU")

# Build lookup: id → list of (product_dict, language, filename)
_all_products: dict[str, list] = {}
for cfg in file_configs:
    prods, _ = extract_products(cfg["rows"], remap)
    for p in prods:
        _all_products.setdefault(p["id"], []).append(
            (p, cfg["language"], cfg["filename"])
        )

_sku_ids = sorted(_all_products.keys())

if _sku_ids:
    col_sku, col_go = st.columns([3, 1])
    with col_sku:
        sku_input = st.selectbox(
            "Select or search SKU (product id)",
            options=_sku_ids,
            index=0,
            key="curl_sku",
        )
    with col_go:
        st.markdown("<br>", unsafe_allow_html=True)
        gen_curl = st.button("Generate cURL", use_container_width=True)

    if gen_curl and sku_input and sku_input in _all_products:
        entries = _all_products[sku_input]
        lang_list = ", ".join(LANGUAGE_OPTIONS.get(e[1], e[1]) for e in entries)
        st.caption(f"**SKU:** `{sku_input}` — found in {len(entries)} file(s): {lang_list}")

        for product, lang, fname in entries:
            is_base = lang in BASE_LANGUAGES
            lang_label = LANGUAGE_OPTIONS.get(lang, lang)

            st.markdown(f"---\n#### {lang_label} (`{lang}`) — {fname}")

            # --- V2 cURL ---
            v2_payload = {"products": [product]}
            v2_body = json.dumps(v2_payload, ensure_ascii=False, indent=2)
            v2_curl = (
                f"curl -X POST '{API_FEED_V2}' \\\n"
                f"  -H 'Content-Type: application/json' \\\n"
                f"  -H 'x-retailer-id: {retailer_id}' \\\n"
                f"  -H 'x-token: {token}' \\\n"
                f"  -d '{v2_body}'"
            )
            st.markdown("**Feed V2** (all fields)")
            st.code(v2_curl, language="bash")

            # --- ML cURL ---
            if not is_base:
                ml_product = ml_only(product)
                ml_payload = {"language": lang, "products": [ml_product]}
                ml_body = json.dumps(ml_payload, ensure_ascii=False, indent=2)
                ml_curl = (
                    f"curl -X POST '{API_MULTI_LANG}' \\\n"
                    f"  -H 'Content-Type: application/json' \\\n"
                    f"  -H 'x-retailer-id: {retailer_id}' \\\n"
                    f"  -H 'x-token: {token}' \\\n"
                    f"  -d '{ml_body}'"
                )
                st.markdown("**Multi-Language** (text fields)")
                st.code(ml_curl, language="bash")
            else:
                st.info(f"ML cURL skipped — `{lang}` is the base language (synced via V2 only).")
else:
    st.caption("Upload feed files above to generate cURL commands.")

st.divider()

# --- Sync ---
st.subheader("Sync")

col_dry, col_sync = st.columns(2)
with col_dry:
    dry_run = st.button("Dry Run (validate only)", use_container_width=True)
with col_sync:
    live_sync = st.button("Sync to Osmos", type="primary", use_container_width=True,
                           disabled=not (retailer_id and token))

if not retailer_id and not token:
    st.caption("Enter API credentials in the sidebar to enable sync.")

# --- Dry Run ---
if dry_run:
    st.subheader("Dry Run Results")
    for cfg in file_configs:
        products, skipped = extract_products(cfg["rows"], remap)
        lang_label = LANGUAGE_OPTIONS.get(cfg["language"], cfg["language"])
        st.success(
            f"**{cfg['filename']}** ({lang_label}): "
            f"{len(products):,} products · {skipped} skipped"
        )
        if products:
            st.caption("Sample V2 payload (all fields):")
            st.code(json.dumps(products[0], ensure_ascii=False, indent=2), language="json")
            st.caption("Sample ML payload (text fields):")
            st.code(json.dumps(ml_only(products[0]), ensure_ascii=False, indent=2), language="json")

# --- Live Sync ---
if live_sync:
    if not retailer_id or not token:
        st.error("Enter Retailer ID and API Token in the sidebar.")
        st.stop()

    st.subheader("Sync Progress")
    all_results = []

    for cfg in file_configs:
        products, skipped = extract_products(cfg["rows"], remap)
        lang_label = LANGUAGE_OPTIONS.get(cfg["language"], cfg["language"])

        is_base = cfg["language"] in BASE_LANGUAGES
        base_note = " ⚡ V2 only (base language)" if is_base else ""
        st.markdown(f"**{cfg['filename']}** — {lang_label} (`{cfg['language']}`){base_note}")

        if not products:
            st.warning(f"No valid products for {cfg['filename']}.")
            continue

        progress_bar = st.progress(0, text="Starting...")
        status_text = st.empty()

        result = run_sync(
            retailer_id=retailer_id, token=token,
            language=cfg["language"], products=products,
            filename=cfg["filename"],
            progress_bar=progress_bar, status_text=status_text,
            skip_ml=is_base,
        )
        result.skipped = skipped
        all_results.append(result)

        v2_status = "✅" if result.v2_failed == 0 else "❌"

        if is_base:
            # Base language — ML skipped
            if result.v2_failed == 0:
                st.success(f"V2: {result.v2_synced:,}/{result.total:,} {v2_status}  |  ML: skipped (base language)")
            else:
                st.error(f"V2: synced {result.v2_synced:,}, failed {result.v2_failed:,} {v2_status}  |  ML: skipped")
        else:
            ml_status = "✅" if result.ml_failed == 0 else "❌"
            if result.v2_failed == 0 and result.ml_failed == 0:
                st.success(f"V2: {result.v2_synced:,}/{result.total:,} {v2_status}  |  "
                           f"ML: {result.ml_synced:,}/{result.total:,} {ml_status}")
            else:
                st.error(f"V2: synced {result.v2_synced:,}, failed {result.v2_failed:,} {v2_status}  |  "
                         f"ML: synced {result.ml_synced:,}, failed {result.ml_failed:,} {ml_status}")

        with st.expander("Sync logs"):
            for line in result.logs:
                st.text(line)

    # --- Summary ---
    st.divider()
    st.subheader("Summary")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Products", f"{sum(r.total for r in all_results):,}")
    c2.metric("V2 Synced", f"{sum(r.v2_synced for r in all_results):,}")
    c3.metric("ML Synced", f"{sum(r.ml_synced for r in all_results):,}")
    c4.metric("Skipped", f"{sum(r.skipped for r in all_results):,}")
