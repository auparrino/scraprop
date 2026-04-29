"""Daily entrypoint: scrape both sites, store, emit a report of NEW listings.

Usage:
    python run_daily.py                 # full run, real scrape
    python run_daily.py --site argenprop
    python run_daily.py --site zonaprop
    python run_daily.py --max-pages 3   # cap pages per site (smoke test)

Output:
    data/listings.db                    SQLite store
    reports/YYYY-MM-DD.md               Daily report (markdown)
    reports/YYYY-MM-DD.json             Daily report (json)
    logs/YYYY-MM-DD.log                 Run log
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

from scraper import argenprop, zonaprop
from scraper.common import Listing
from scraper.storage import Store


ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"
LOGS_DIR = ROOT / "logs"
VIEWER_DIR = ROOT / "viewer"


def _setup_logging(today: str) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / f"{today}.log"
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}


def _format_money(usd: int | None) -> str:
    return f"USD {usd:,.0f}".replace(",", ".") if usd else "-"


def _write_report(today: str, new_rows, repub_rows, run_summary: dict) -> tuple[Path, Path]:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    md_path = REPORTS_DIR / f"{today}.md"
    json_path = REPORTS_DIR / f"{today}.json"

    md = [f"# Departamentos nuevos — {today}", ""]
    md.append(f"- Total scrapeados hoy: **{run_summary['total_seen']}**")
    md.append(f"- Nuevos (primera vez vistos, no republicaciones): **{len(new_rows)}**")
    md.append(f"- Republicaciones detectadas: **{len(repub_rows)}**")
    md.append("")

    if new_rows:
        md.append("## Nuevos del día")
        md.append("")
        md.append("| Fuente | Precio | m² | Barrio | Dirección | Link |")
        md.append("|---|---|---|---|---|---|")
        for r in new_rows:
            md.append(
                f"| {r['source']} "
                f"| {_format_money(r['price_usd'])} "
                f"| {r['m2'] or '-'} "
                f"| {r['barrio'] or '-'} "
                f"| {(r['address'] or '-').replace('|', '/')} "
                f"| [link]({r['url']}) |"
            )
        md.append("")
    else:
        md.append("## Nuevos del día\n\n_No hubo listings nuevos hoy._\n")

    if repub_rows:
        md.append("## Republicaciones (NO enviadas)")
        md.append("")
        md.append("| Fuente | Precio | Dirección | Republica de | Link |")
        md.append("|---|---|---|---|---|")
        for r in repub_rows:
            md.append(
                f"| {r['source']} "
                f"| {_format_money(r['price_usd'])} "
                f"| {(r['address'] or '-').replace('|', '/')} "
                f"| {r['republish_of'] or '-'} "
                f"| [link]({r['url']}) |"
            )
        md.append("")

    md_path.write_text("\n".join(md), encoding="utf-8")

    payload = {
        "date": today,
        "summary": run_summary,
        "new": [_row_to_dict(r) for r in new_rows],
        "republishes": [_row_to_dict(r) for r in repub_rows],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                         encoding="utf-8")
    return md_path, json_path


_INLINE_DATA_RE = re.compile(
    r'(<script id="scraprop-data" type="application/json">)(.*?)(</script>)',
    re.DOTALL,
)


def _inject_inline_data(index_path: Path, raw_json: str) -> None:
    """Replace the inline JSON block in viewer/index.html with the latest data.

    Makes the viewer self-contained: opening index.html alone (no data.js, no http server,
    no sandbox-side fetches) shows up-to-date listings.
    """
    if not index_path.exists():
        return
    html = index_path.read_text(encoding="utf-8")
    # JSON inside <script type="application/json"> just needs </script> escaped.
    safe = raw_json.replace("</script>", "<\\/script>")
    new_html, n = _INLINE_DATA_RE.subn(
        lambda m: m.group(1) + safe + m.group(3), html, count=1
    )
    if n:
        index_path.write_text(new_html, encoding="utf-8")


def _export_viewer_data(store, today: str) -> Path:
    """Write viewer/data.json + data.js + inject inline data into viewer/index.html."""
    VIEWER_DIR.mkdir(parents=True, exist_ok=True)
    rows = store.all_active_listings()
    listings = []
    for r in rows:
        listings.append({
            "listing_id": r["listing_id"],
            "source": r["source"],
            "url": r["url"],
            "title": r["title"],
            "address": r["address"],
            "barrio": r["barrio"],
            "price_usd": r["price_usd"],
            "expensas_ars": r["expensas_ars"],
            "m2": r["m2"],
            "ambientes": r["ambientes"],
            "dormitorios": r["dormitorios"],
            "banos": r["banos"],
            "antiguedad": r["antiguedad"],
            "orientacion": r["orientacion"],
            "orientacion_cardinal": r["orientacion_cardinal"],
            "description": r["description"],
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
            "times_seen": r["times_seen"],
        })
    payload = {
        "generated_at": today,
        "generated_at_time": datetime.now().strftime("%H:%M"),
        "listings": listings,
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    out = VIEWER_DIR / "data.json"
    out.write_text(raw, encoding="utf-8")
    # Also dump as data.js (window.__SCRAPROP__) for file:// fallback
    js_path = VIEWER_DIR / "data.js"
    js_path.write_text("window.__SCRAPROP__ = " + raw + ";\n", encoding="utf-8")
    # And inject inline into index.html so it's a fully self-contained file
    _inject_inline_data(VIEWER_DIR / "index.html", raw)
    return out


def run(*, site: str | None, max_pages: int | None, headless: bool) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    started_at = datetime.now().isoformat(timespec="seconds")
    _setup_logging(today)
    log = logging.getLogger("run")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    store = Store(DATA_DIR / "listings.db")
    store.start_run(today, started_at)

    counters = {"total_seen": 0, "new": 0, "new_republish": 0, "seen": 0}
    sources = []
    if site in (None, "argenprop"):
        sources.append(("argenprop", argenprop))
    if site in (None, "zonaprop"):
        sources.append(("zonaprop", zonaprop))

    try:
        for name, mod in sources:
            log.info("=== Scraping %s ===", name)
            try:
                if name == "argenprop":
                    iterator = mod.scrape(
                        max_pages_per_barrio=max_pages or 50,
                    )
                else:
                    iterator = mod.scrape(
                        max_pages=max_pages or 50,
                        headless=headless,
                    )
                for listing in iterator:
                    counters["total_seen"] += 1
                    result = store.upsert(listing, today)
                    counters[result["status"]] = counters.get(result["status"], 0) + 1
                    if result["status"] == "new":
                        log.info("NEW %s | %s | USD %s | %s",
                                 listing.source, listing.address,
                                 listing.price_usd, listing.url)
                    elif result["status"] == "new_republish":
                        log.info("REPUBLISH %s | of %s | %s",
                                 listing.listing_id, result["republish_of"], listing.url)
            except Exception as e:
                log.exception("Scraper %s failed: %s", name, e)
    finally:
        new_rows = store.listings_first_seen_on(today, exclude_republish=True)
        repub_rows = store.republishes_first_seen_on(today)

        finished_at = datetime.now().isoformat(timespec="seconds")
        store.finish_run(
            today, finished_at,
            total_seen=counters["total_seen"],
            new_listings=len(new_rows),
            republishes=len(repub_rows),
            notes=f"counters={counters}",
        )

        run_summary = {
            "total_seen": counters["total_seen"],
            "new": len(new_rows),
            "republishes": len(repub_rows),
            "started_at": started_at,
            "finished_at": finished_at,
        }
        md_path, json_path = _write_report(today, new_rows, repub_rows, run_summary)
        viewer_data = _export_viewer_data(store, today)
        log.info("Run summary: %s", run_summary)
        log.info("Report: %s", md_path)
        log.info("Report JSON: %s", json_path)
        log.info("Viewer data: %s (%s listings)", viewer_data, len(store.all_active_listings()))
        store.close()

    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--site", choices=["argenprop", "zonaprop"], default=None,
                   help="Scrape only one source")
    p.add_argument("--max-pages", type=int, default=None,
                   help="Cap pages per site (useful for smoke tests)")
    p.add_argument("--no-headless", action="store_true",
                   help="Run zonaprop browser visibly (debug)")
    args = p.parse_args()
    return run(site=args.site, max_pages=args.max_pages, headless=not args.no_headless)


if __name__ == "__main__":
    raise SystemExit(main())
