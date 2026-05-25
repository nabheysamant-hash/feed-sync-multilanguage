"""
Osmos Multi-Language Feed Sync (CLI)

Syncs per-language product feed files via the Multi-Language API.
All columns from the CSV are sent as-is.

Usage:
    python sync.py                     # sync all languages
    python sync.py --language ar       # sync only Arabic feed
    python sync.py --dry-run           # validate without calling API
"""

import argparse
import csv
import json
import logging
import time
from pathlib import Path
from typing import Iterator

import requests
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

API_ENDPOINT = "https://apiv2.onlinesales.ai/catalogSyncService/products/multiLanguage"
REQUIRED_FIELDS = {"id", "title", "description", "brand"}

DEFAULT_REMAP = {
    "image_url": "image_link",
}

_STRIP_CHARS = str.maketrans("", "", "\r\0\t")


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _remap_row(row: dict, remap: dict) -> dict:
    out = {}
    for col, raw in row.items():
        if col is None:
            continue
        api_name = remap.get(col, col)
        value = (raw or "").strip().translate(_STRIP_CHARS)
        if value:
            out[api_name] = value
    return out


def read_products(filepath: str, remap: dict) -> Iterator[dict]:
    """Yield ALL columns from CSV as product dicts — no field filtering."""
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {filepath}")

    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","

    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=delimiter)

        for row_num, row in enumerate(reader, start=2):
            mapped = _remap_row(row, remap)
            missing = REQUIRED_FIELDS - mapped.keys()
            if missing:
                log.warning("Row %d skipped — missing: %s (id=%s)",
                            row_num, sorted(missing), mapped.get("id", "?"))
                continue
            yield mapped


def _batches(items: Iterator[dict], size: int) -> Iterator[list]:
    batch = []
    for item in items:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def _send_batch(session, endpoint, headers, language, products, retry_attempts, retry_delay):
    payload = {"language": language, "products": products}

    for attempt in range(1, retry_attempts + 1):
        try:
            resp = session.post(endpoint, json=payload, headers=headers, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                log.info("  [OK]  request_id=%s  batch=%d",
                         data.get("request_id", "?"), len(products))
                return True
            try:
                error = resp.json().get("error", {})
                log.warning("  [FAIL] attempt=%d/%d  http=%d  code=%s  msg=%s",
                            attempt, retry_attempts, resp.status_code,
                            error.get("code"), error.get("message"))
            except Exception:
                log.warning("  [FAIL] attempt=%d/%d  http=%d  body=%s",
                            attempt, retry_attempts, resp.status_code, resp.text[:200])
        except requests.RequestException as exc:
            log.warning("  [ERROR] attempt=%d/%d  %s", attempt, retry_attempts, exc)
        if attempt < retry_attempts:
            time.sleep(retry_delay)
    return False


def sync_file(config, filepath, language, dry_run=False):
    api = config["api"]
    cfg = config["sync"]
    remap = config.get("remap", DEFAULT_REMAP)

    endpoint = api.get("endpoint", API_ENDPOINT)
    headers = {
        "x-retailer-id": str(api["retailer_id"]),
        "x-token": str(api["token"]),
        "Content-Type": "application/json",
    }
    batch_size = int(cfg.get("batch_size", 50))
    rate_limit = float(cfg.get("rate_limit", 10))
    retry_attempts = int(cfg.get("retry_attempts", 3))
    retry_delay = float(cfg.get("retry_delay", 2))
    min_interval = 1.0 / rate_limit

    log.info("==> Syncing  language=%s  file=%s  dry_run=%s", language, filepath, dry_run)

    products_iter = read_products(filepath, remap)

    if dry_run:
        products = list(products_iter)
        log.info("    [DRY RUN] %d products, %d fields each",
                 len(products), len(products[0]) if products else 0)
        if products:
            log.info("    Fields: %s", sorted(products[0].keys()))
            log.info("    Sample: %s", json.dumps(products[0], ensure_ascii=False)[:300])
        return len(products), 0

    session = requests.Session()
    total = 0
    failed = 0
    last_sent = 0.0

    for batch in _batches(products_iter, batch_size):
        wait = min_interval - (time.monotonic() - last_sent)
        if wait > 0:
            time.sleep(wait)
        ok = _send_batch(session, endpoint, headers, language, batch, retry_attempts, retry_delay)
        last_sent = time.monotonic()
        total += len(batch)
        if not ok:
            failed += len(batch)

    log.info("    Done  language=%s  synced=%d  failed=%d  total=%d",
             language, total - failed, failed, total)
    return total, failed


def main():
    parser = argparse.ArgumentParser(description="Osmos multi-language feed sync")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--language", help="Sync only this language code (e.g. 'ar')")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = load_config(args.config)
    lang_configs = config.get("languages", [])

    if args.language:
        lang_configs = [lc for lc in lang_configs if lc["language"] == args.language]
        if not lang_configs:
            log.error("Language '%s' not in config.", args.language)
            raise SystemExit(1)

    overall_total = 0
    overall_failed = 0

    for lc in lang_configs:
        try:
            total, failed = sync_file(config, lc["file"], lc["language"], dry_run=args.dry_run)
            overall_total += total
            overall_failed += failed
        except Exception as exc:
            log.error("Error syncing %s: %s", lc["file"], exc)

    log.info("\nSummary: %d/%d products across %d language(s)",
             overall_total - overall_failed, overall_total, len(lang_configs))


if __name__ == "__main__":
    main()
