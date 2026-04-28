"""Argenprop scraper — plain HTTP + BeautifulSoup."""
from __future__ import annotations

import logging
import re
import time
from typing import Iterator, List

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

from .common import (
    Listing, detect_barrio, detect_antiguedad, detect_orientacion,
    parse_int, matches_filters, proxy_wrap,
    PRICE_USD_MIN, PRICE_USD_MAX, TARGET_AMBIENTES,
)


log = logging.getLogger("argenprop")
BASE = "https://www.argenprop.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _list_url(barrio_slug: str, ambientes: int, page: int) -> str:
    """Argenprop URL pattern, ordenado por más nuevos primero."""
    base = (f"{BASE}/departamentos/venta/{barrio_slug}/{ambientes}-ambientes/"
            f"dolares-desde-{PRICE_USD_MIN}-hasta-{PRICE_USD_MAX}")
    qs = "orden-masnuevos" if page <= 1 else f"pagina-{page}&orden-masnuevos"
    return f"{base}?{qs}"


def _parse_card(card) -> Listing | None:
    """Parse one .listing__items > .listing__item card. Return None if not parseable."""
    a = card.select_one("a.card")
    if not a:
        return None

    external_id = a.get("data-item-card") or card.get("id") or ""
    if not external_id:
        return None

    href = a.get("href", "")
    url = href if href.startswith("http") else f"{BASE}{href}"

    # Address
    addr_el = a.select_one(".card__address")
    address = addr_el.get_text(" ", strip=True) if addr_el else ""

    # Title (used as description in argenprop cards)
    title_el = a.select_one(".card__title")
    title = title_el.get_text(" ", strip=True) if title_el else ""

    # Price (USD)
    price_el = a.select_one(".card__price")
    price_text = price_el.get_text(" ", strip=True) if price_el else ""
    # data-* attribute is more reliable than parsing text
    price_usd = parse_int(a.get("montonormalizado") or a.get("montooperacion") or "")
    if price_usd is None and "USD" in price_text.upper():
        price_usd = parse_int(price_text.split("+")[0])

    # Expensas (ARS) — appears as "+ $200.000 expensas"
    exp_el = a.select_one(".card__expenses")
    expensas_ars = parse_int(exp_el.get_text(" ", strip=True)) if exp_el else None

    # Features: "78 m² cubie. 2 dorm. 45 años"
    feats_el = a.select_one(".card__main-features")
    feats_text = feats_el.get_text(" ", strip=True) if feats_el else ""
    m2 = None
    dorms = None
    banos = None
    if feats_el:
        for li in feats_el.select("li"):
            t = li.get_text(" ", strip=True).lower()
            if "m" in t and ("²" in t or "2" in t):
                m2 = m2 or parse_int(t)
            if "dorm" in t:
                dorms = dorms or parse_int(t)
            if "baño" in t or "bano" in t or "bañ" in t:
                banos = banos or parse_int(t)
        # fallback regex
        if m2 is None:
            m2 = parse_int(feats_text)
        if banos is None:
            mb = re.search(r"(\d+)\s*ba[nñ]", feats_text, re.IGNORECASE)
            if mb:
                banos = int(mb.group(1))

    # ambientes — leemos el atributo "ambientes" del card (más confiable);
    # si no aparece, lo inferimos del slug de la URL.
    amb_attr = a.get("ambientes")
    ambientes = parse_int(amb_attr) if amb_attr else None
    if ambientes is None:
        m_amb = re.search(r"(\d+)-ambientes", url)
        if m_amb:
            ambientes = int(m_amb.group(1))

    # Barrio: try card text + address + URL slug
    location_blob = " ".join([
        address,
        a.get_text(" ", strip=True)[:300],
        url,
    ])
    barrio = detect_barrio(location_blob)

    raw_text = a.get_text(" ", strip=True)
    return Listing(
        source="argenprop",
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
        banos=banos,
        antiguedad=detect_antiguedad(feats_text + " " + title),
        orientacion=detect_orientacion(title + " " + raw_text),
        description=title,
        raw_text=raw_text,
    )


def _fetch(url: str, session, retries: int = 4) -> str | None:
    """Fetch with Chrome TLS impersonation. Argenprop screens data-center IPs with HTTP 202
    (a soft challenge); curl_cffi + browser-like fingerprint clears it most of the time."""
    fetch_url = proxy_wrap(url)
    for attempt in range(1, retries + 1):
        try:
            r = session.get(fetch_url, impersonate="chrome", timeout=60)
            if r.status_code == 200:
                return r.text
            log.warning("argenprop %s -> HTTP %s (attempt %s)", url, r.status_code, attempt)
            # 202 = anti-bot "interstitial". A short wait sometimes lets us through on retry.
            if r.status_code in (202, 429, 403):
                time.sleep(3.0 * attempt)
                continue
        except Exception as e:
            log.warning("argenprop %s -> %s (attempt %s)", url, e, attempt)
        time.sleep(1.5 * attempt)
    return None


def scrape(*, max_pages_per_barrio: int = 50,
           barrios: tuple = ("caballito", "villa-crespo"),
           delay: float = 0.6) -> Iterator[Listing]:
    """Yield Listings from argenprop matching the configured filters."""
    session = cffi_requests.Session()
    session.headers.update(HEADERS)
    seen_ids: set[str] = set()
    target_ambientes = TARGET_AMBIENTES if isinstance(TARGET_AMBIENTES, tuple) else (TARGET_AMBIENTES,)
    for barrio_slug in barrios:
        for amb in target_ambientes:
            for page in range(1, max_pages_per_barrio + 1):
                url = _list_url(barrio_slug, amb, page)
                html = _fetch(url, session)
                if not html:
                    log.warning("argenprop: stopping %s/%samb at page %s (fetch failed)",
                                barrio_slug, amb, page)
                    break
                soup = BeautifulSoup(html, "html.parser")
                cards = soup.select(".listing__items > .listing__item")
                log.info("argenprop %s %samb page %s -> %s cards",
                         barrio_slug, amb, page, len(cards))
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
                time.sleep(delay)
