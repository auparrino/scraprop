"""Shared helpers: parsing, normalization, fingerprinting."""
from __future__ import annotations

import hashlib
import os
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass, asdict, field
from typing import Optional


def proxy_wrap(url: str) -> str:
    """If SCRAPER_API_KEY is set, route the request via ScraperAPI with AR residential IPs.
    Otherwise return the URL unchanged."""
    key = os.environ.get("SCRAPER_API_KEY")
    if not key:
        return url
    return ("https://api.scraperapi.com/"
            f"?api_key={key}&country_code=ar&keep_headers=true"
            f"&url={urllib.parse.quote(url, safe='')}")


# Filter constants — single source of truth
TARGET_BARRIOS = ("caballito", "villa crespo")
TARGET_AMBIENTES = 3
PRICE_USD_MIN = 135_000
PRICE_USD_MAX = 170_000


@dataclass
class Listing:
    source: str            # "zonaprop" | "argenprop"
    external_id: str       # site-specific id
    url: str               # absolute URL
    title: str = ""
    address: str = ""      # street + number
    barrio: str = ""       # normalized neighborhood
    price_usd: Optional[int] = None
    expensas_ars: Optional[int] = None
    m2: Optional[int] = None
    ambientes: Optional[int] = None
    dormitorios: Optional[int] = None
    antiguedad: Optional[str] = None     # e.g. "45 años", "a estrenar", "en pozo"
    orientacion: Optional[str] = None    # "frente" | "contrafrente" | "lateral" | "interno"
    description: str = ""
    raw_text: str = field(default="", repr=False)

    @property
    def listing_id(self) -> str:
        return f"{self.source}:{self.external_id}"

    @property
    def fingerprint(self) -> str:
        """Cross-source dedup key for republishes.

        Same physical property reposted (same site or other site) tends to share
        address+m2+rooms. Price can drift, so we bucket it. Address is normalized.
        """
        parts = [
            normalize_address(self.address),
            str(self.m2 or 0),
            str(self.ambientes or 0),
            str(self.dormitorios or 0),
            normalize_text(self.barrio),
            # 5k USD bucket — small price tweaks shouldn't break matching
            str((self.price_usd or 0) // 5000),
        ]
        return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("raw_text", None)
        return d


_NUM_RE = re.compile(r"[\d\.,]+")


def parse_int(text: str) -> Optional[int]:
    """Parse 'USD 158.000', '78 m²', '$ 120.000' → integer."""
    if not text:
        return None
    m = _NUM_RE.search(text)
    if not m:
        return None
    raw = m.group(0).replace(".", "").replace(",", "")
    try:
        return int(raw)
    except ValueError:
        return None


def normalize_text(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower().strip()
    t = re.sub(r"\s+", " ", t)
    return t


_ADDR_NOISE = re.compile(r"\b(piso|depto|dpto|dto|of|oficina|uf)\b.*$", re.IGNORECASE)
_PUNCT_RE = re.compile(r"[^\w\s]")


def normalize_address(address: str) -> str:
    """Best-effort canonicalization so 'Acoyte 200, Piso 2' and 'Acoyte 200' match."""
    if not address:
        return ""
    a = normalize_text(address)
    a = a.split(",")[0]                # drop ", Piso 2" etc.
    a = _ADDR_NOISE.sub("", a)
    a = _PUNCT_RE.sub(" ", a)
    a = re.sub(r"\s+", " ", a).strip()
    return a


_ANTIG_RE = re.compile(
    r"(a\s+estrenar|en\s+pozo|en\s+construcci[oó]n|\d+\s*a[nñ]os?)",
    re.IGNORECASE,
)


def detect_antiguedad(text: str) -> Optional[str]:
    """Pull antiquity from card text. Returns 'a estrenar', 'en pozo', 'en construcción', or 'X años'."""
    if not text:
        return None
    m = _ANTIG_RE.search(text)
    if not m:
        return None
    val = m.group(1).strip().lower()
    val = re.sub(r"\s+", " ", val)
    return val


_ORIENT_TOKENS = (
    ("contrafrente", "contrafrente"),
    ("contra frente", "contrafrente"),
    ("al frente", "frente"),
    (" frente ", "frente"),
    ("frente luminoso", "frente"),
    ("lateral", "lateral"),
    ("interno", "interno"),
    ("al cfte", "contrafrente"),
)


def detect_orientacion(text: str) -> Optional[str]:
    if not text:
        return None
    t = " " + normalize_text(text) + " "
    for needle, label in _ORIENT_TOKENS:
        if needle in t:
            return label
    return None


def detect_barrio(text: str) -> str:
    """Return canonical barrio name if present in text, else ''."""
    t = normalize_text(text)
    if "villa crespo" in t:
        return "villa crespo"
    if "caballito" in t:
        return "caballito"
    return ""


def matches_filters(l: Listing) -> bool:
    """Final guard: enforce 3-amb, target barrios, USD 100K-170K."""
    if l.ambientes is not None and l.ambientes != TARGET_AMBIENTES:
        return False
    if l.price_usd is None:
        return False
    if not (PRICE_USD_MIN <= l.price_usd <= PRICE_USD_MAX):
        return False
    if l.barrio not in TARGET_BARRIOS:
        return False
    return True
