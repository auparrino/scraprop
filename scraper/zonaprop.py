"""Zonaprop scraper — uses curl_cffi (Chrome TLS impersonation) to pass Cloudflare."""
from __future__ import annotations

import logging
import random
import time
from typing import Iterator

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

from .common import (
    Listing, detect_barrio, detect_antiguedad, detect_orientacion,
    parse_int, matches_filters, TARGET_AMBIENTES,
    PRICE_USD_MIN, PRICE_USD_MAX,
)


log = logging.getLogger("zonaprop")
BASE = "https://www.zonaprop.com.ar"


def _list_url(page: int) -> str:
    """Caballito + Villa Crespo, 3 amb, $100K-170K USD, sorted newest first."""
    base = (f"{BASE}/departamentos-venta-caballito-villa-crespo-"
            f"{TARGET_AMBIENTES}-ambientes-mas-{PRICE_USD_MIN}-menos-{PRICE_USD_MAX}-dolar")
    sort = "-orden-publicado-descendente"
    if page <= 1:
        return f"{base}{sort}.html"
    return f"{base}{sort}-pagina-{page}.html"


def _parse_card(card) -> Listing | None:
    external_id = card.get("data-id")
    href = card.get("data-to-posting") or ""
    if not external_id or not href:
        return None
    clean_path = href.split("?")[0]
    url = clean_path if clean_path.startswith("http") else f"{BASE}{clean_path}"

    price_el = card.select_one('[data-qa="POSTING_CARD_PRICE"]')
    price_text = price_el.get_text(" ", strip=True) if price_el else ""
    price_usd = parse_int(price_text) if "USD" in price_text.upper() else None

    exp_el = card.select_one('[data-qa="expensas"]')
    expensas_ars = parse_int(exp_el.get_text(" ", strip=True)) if exp_el else None

    feats_el = card.select_one('[data-qa="POSTING_CARD_FEATURES"]')
    feats_text = feats_el.get_text(" ", strip=True) if feats_el else ""
    m2 = ambientes = dorms = None
    if feats_el:
        spans = [s.get_text(" ", strip=True) for s in feats_el.select("span")]
        chunks = spans or feats_text.split()
        for chunk in chunks:
            low = chunk.lower()
            if "m" in low and ("²" in low or "tot" in low or "cub" in low):
                m2 = m2 or parse_int(chunk)
            elif "amb" in low:
                ambientes = ambientes or parse_int(chunk)
            elif "dorm" in low:
                dorms = dorms or parse_int(chunk)
        if m2 is None:
            m2 = parse_int(feats_text)

    addr_el = card.select_one(".postingLocations-module__location-address")
    address = addr_el.get_text(" ", strip=True) if addr_el else ""

    loc_el = card.select_one('[data-qa="POSTING_CARD_LOCATION"]')
    location_text = loc_el.get_text(" ", strip=True) if loc_el else ""

    desc_el = card.select_one('[data-qa="POSTING_CARD_DESCRIPTION"]')
    description = desc_el.get_text(" ", strip=True) if desc_el else ""
    title = description[:120].strip()

    barrio = detect_barrio(" ".join([location_text, address, url]))

    raw_text = card.get_text(" ", strip=True)
    return Listing(
        source="zonaprop",
        external_id=external_id,
        url=url,
        title=title,
        address=address,
        barrio=barrio,
        price_usd=price_usd,
        expensas_ars=expensas_ars,
        m2=m2,
        ambientes=ambientes,
        dormitorios=dorms,
        antiguedad=detect_antiguedad(feats_text + " " + description),
        orientacion=detect_orientacion(description + " " + raw_text),
        description=description,
        raw_text=raw_text,
    )


def _fetch(url: str, session, retries: int = 3):
    """Fetch a Zonaprop URL. curl_cffi impersonates Chrome's TLS to pass Cloudflare."""
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, impersonate="chrome", timeout=30)
            if r.status_code == 200:
                return r.text
            log.warning("zonaprop %s -> HTTP %s (attempt %s)", url, r.status_code, attempt)
        except Exception as e:
            log.warning("zonaprop %s -> %s (attempt %s)", url, e, attempt)
        time.sleep(random.uniform(2.0, 5.0) * attempt)
    return None


def scrape(*, max_pages: int = 50,
           delay_min: float = 1.0, delay_max: float = 2.5,
           # accepted (and ignored) for compat with the runner's signature
           headless: bool = True) -> Iterator[Listing]:
    """Yield Listings from zonaprop matching the configured filters."""
    seen_ids: set[str] = set()
    session = cffi_requests.Session()
    # Set sane default headers; curl_cffi already adds Chrome-like headers via impersonate
    session.headers.update({
        "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
    })

    for page_num in range(1, max_pages + 1):
        url = _list_url(page_num)
        html = _fetch(url, session)
        if not html:
            log.warning("zonaprop: stopping at page %s (fetch failed)", page_num)
            break

        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select("div[data-id][data-posting-type='PROPERTY']")
        log.info("zonaprop page %s -> %s cards", page_num, len(cards))
        if not cards:
            break

        for card in cards:
            listing = _parse_card(card)
            if listing is None:
                continue
            if listing.listing_id in seen_ids:
                continue
            if not matches_filters(listing):
                continue
            seen_ids.add(listing.listing_id)
            yield listing

        if len(cards) < 20:
            break
        time.sleep(random.uniform(delay_min, delay_max))
