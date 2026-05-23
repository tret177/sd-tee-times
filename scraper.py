"""
San Diego Tee Time Scraper
==========================
Scrapes tee time availability for multiple San Diego golf courses:

  - **City munis** (ForeUp): Mission Bay, Balboa Park, Torrey Pines N + S
  - **Coronado Municipal** (TeeItUp / Kenna backend)

Two vendors, two API surfaces, but one normalized output (`TeeTime`).

────────────────────────────────────────────────────────────────────────────
ForeUp (city munis)

    GET https://foreupsoftware.com/index.php/api/booking/times
        ?time=all
        &date=MM-DD-YYYY            # note: M-D-Y, NOT ISO
        &holes=18
        &players=4
        &schedule_id={teesheet_id}
        &booking_class={class_id}
        &course_id={course_id}

Each city course has one or more "teesheets" and each teesheet has multiple
"booking classes" (Resident 0-7d, Non-Resident 0-90d, Standard, Advance,
Junior, etc). Each booking class gates a slice of available times and may
carry different green fees. We query every relevant (teesheet, class)
combination and aggregate.

Mapping (verified 2026-05-13 from each booking page's embedded
booking_classes JSON):

  Mission Bay  course_id=19346, teesheet 1469
  Torrey Pines course_id=19347, teesheets 1487 (South) + 1468 (North)
  Balboa Park  course_id=19348, teesheet 1470

────────────────────────────────────────────────────────────────────────────
TeeItUp / Kenna (Coronado)

    GET https://phx-api-be-east-1b.kenna.io/v2/tee-times
        ?date=YYYY-MM-DD
        &facilityIds={facility_id}
    Header:  x-be-alias: <tenant-alias>  (from the booking subdomain)

Different shape entirely: returns one object per facility with a `teetimes`
array; each slot has UTC ISO times and a `rates` array where prices live
in `greenFeeCart` cents.

Mapping (verified 2026-05-22 from coronado-gc-3-14-be.book.teeitup.com):

  Coronado Municipal  facility_id=10985, tenant alias 'coronado-gc-3-14-be'

────────────────────────────────────────────────────────────────────────────
If either vendor restructures, follow `inspect_endpoint.md` to refresh.

Usage
-----
    python scraper.py --days 14 --players 4
    python scraper.py --dates 2026-06-01,2026-06-08 --json > times.json
"""

from __future__ import annotations
import argparse
import datetime as dt
import json
import logging
import sys
import time
from dataclasses import dataclass, asdict, field
from typing import Optional
from zoneinfo import ZoneInfo

import requests


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

FOREUP_BASE = "https://foreupsoftware.com"
API_PATH = "/index.php/api/booking/times"

# ── Kenna (TeeItUp backend) ──────────────────────────────────────────────
KENNA_BASE = "https://phx-api-be-east-1b.kenna.io"

# Each Kenna scrape unit is one API call per (facility, date). The tenant
# alias maps to the booking subdomain (e.g. coronado-gc-3-14-be.book.teeitup.com).
KENNA_UNITS = [
    {
        "course_key": "coronado",
        "course_name": "Coronado Municipal Golf Course",
        "facility_id": 10985,
        "tenant_alias": "coronado-gc-3-14-be",
        "timezone": "America/Los_Angeles",
        "booking_url": "https://coronado-gc-3-14-be.book.teeitup.com/",
    },
]

# ── ForeUp (city munis) ──────────────────────────────────────────────────
# Each scrape unit is one API call: (course, teesheet, booking_class).
# `class_label` is for human display; `residency` segments prices in output.
SCRAPE_UNITS = [
    # Mission Bay (one teesheet, no resident/non-resident split — same price for all)
    {"course_id": 19346, "schedule_id": 1469, "booking_class_id": 925,
     "class_label": "Standard (0-7 days)", "residency": "any"},
    {"course_id": 19346, "schedule_id": 1469, "booking_class_id": 1043,
     "class_label": "Advance (8-90 days)", "residency": "any"},

    # Torrey Pines — teesheet 1487 (one of N/S; the API's schedule_name tells us which)
    {"course_id": 19347, "schedule_id": 1487, "booking_class_id": 888,
     "class_label": "Resident (0-7 days)", "residency": "resident"},
    {"course_id": 19347, "schedule_id": 1487, "booking_class_id": 3195,
     "class_label": "Resident (8-90 days)", "residency": "resident"},
    {"course_id": 19347, "schedule_id": 1487, "booking_class_id": 3088,
     "class_label": "Non-Resident (0-90 days)", "residency": "non_resident"},

    # Torrey Pines — teesheet 1468 (the other of N/S)
    {"course_id": 19347, "schedule_id": 1468, "booking_class_id": 1135,
     "class_label": "Resident (0-7 days)", "residency": "resident"},
    {"course_id": 19347, "schedule_id": 1468, "booking_class_id": 3201,
     "class_label": "Resident (8-90 days)", "residency": "resident"},
    {"course_id": 19347, "schedule_id": 1468, "booking_class_id": 3181,
     "class_label": "Non-Resident (0-90 days)", "residency": "non_resident"},

    # Balboa Park 18-hole
    {"course_id": 19348, "schedule_id": 1470, "booking_class_id": 929,
     "class_label": "Standard (0-7 days)", "residency": "any"},
    {"course_id": 19348, "schedule_id": 1470, "booking_class_id": 51735,
     "class_label": "Advance (8-90 days)", "residency": "any"},
]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT = 20
INTER_REQUEST_DELAY = 1.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sd_scraper")


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ClassPrice:
    """Pricing for one booking class that has this slot available."""
    booking_class_id: int
    class_label: str
    residency: str          # "resident" | "non_resident" | "any"
    green_fee: Optional[float]


@dataclass
class TeeTime:
    """A single available tee time slot, after merging across booking classes."""
    course_key: str         # dashboard key: torrey_north | torrey_south | balboa | mission_bay
    course_name: str
    date: str               # ISO yyyy-mm-dd
    time: str               # 24h HH:MM
    price: Optional[float]  # the "headline" price (non-resident if available, else green_fee)
    resident_price: Optional[float]
    non_resident_price: Optional[float]
    available_spots: int
    holes: int = 18
    booking_url: str = ""
    teesheet_id: Optional[int] = None
    schedule_name: str = ""
    classes: list[ClassPrice] = field(default_factory=list)
    raw: list[dict] = field(default_factory=list, repr=False)


@dataclass
class ScrapeResult:
    """
    A scrape over one or more dates.

    `date` is the first date scraped (kept for backward-compat readers);
    `dates_scraped` is the authoritative list of every ISO date covered.
    """
    date: str
    dates_scraped: list[str]
    players: int
    scraped_at: str
    tee_times: list[TeeTime]
    errors: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "date": self.date,
                "dates_scraped": self.dates_scraped,
                "players": self.players,
                "scraped_at": self.scraped_at,
                "tee_times": [asdict(t) for t in self.tee_times],
                "errors": self.errors,
            },
            indent=2,
            default=str,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Scraper
# ─────────────────────────────────────────────────────────────────────────────

class ForeUpScraper:
    """Hits ForeUp's public JSON endpoint."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-US,en;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "X-FU-Golfer-Location": "foreup",
        })

    def fetch(self, unit: dict, date_iso: str, players: int, holes: int) -> list[dict]:
        date_us = _iso_to_us(date_iso)
        params = {
            "time": "all",
            "date": date_us,
            "holes": holes,
            "players": players,
            "schedule_id": unit["schedule_id"],
            "booking_class": unit["booking_class_id"],
            "course_id": unit["course_id"],
        }
        referer = f"{FOREUP_BASE}/index.php/booking/{unit['course_id']}"
        r = self.session.get(
            FOREUP_BASE + API_PATH,
            params=params,
            headers={"Referer": referer},
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code in (401, 403):
            # Resident-only classes (e.g. Torrey Pines residents) require a logged-in
            # Resident ID. Without credentials we cannot see them. Treat as empty,
            # not an error.
            log.debug("  auth-gated: HTTP %s for class %s — skipping",
                      r.status_code, unit["booking_class_id"])
            return []
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(f"Non-JSON response: {r.text[:200]}")
        if isinstance(data, dict):
            # ForeUp occasionally wraps in {"times":[...]} or similar
            for key in ("times", "tee_times", "data", "result", "results", "items"):
                if isinstance(data.get(key), list):
                    return data[key]
            log.debug("Unexpected dict shape, keys=%s", list(data.keys())[:8])
            return []
        return data if isinstance(data, list) else []


# ─────────────────────────────────────────────────────────────────────────────
# Kenna scraper (TeeItUp backend — Coronado)
# ─────────────────────────────────────────────────────────────────────────────

class KennaScraper:
    """
    Hits Kenna's public /v2/tee-times endpoint. Different shape than ForeUp:
    one call per (facility, date), response is one object per facility with
    a `teetimes` array; each slot has UTC times and a `rates` array (prices
    in cents under `greenFeeCart`).

    We emit one TeeTime per slot, taking the lowest visible rate as the
    headline price. (Unauthenticated callers typically only see the
    non-resident/public rate anyway — resident-only rates require login.)
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        })

    def fetch_one(self, unit: dict, date_iso: str) -> list[dict]:
        """Fetch raw `teetimes` array for one facility on one date."""
        headers = {
            "x-be-alias": unit["tenant_alias"],
            "Origin": f"https://{unit['tenant_alias']}.book.teeitup.com",
            "Referer": f"https://{unit['tenant_alias']}.book.teeitup.com/",
        }
        r = self.session.get(
            KENNA_BASE + "/v2/tee-times",
            params={"date": date_iso, "facilityIds": str(unit["facility_id"])},
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code in (401, 403):
            log.debug("  Kenna auth-gated for facility %s — skipping",
                      unit["facility_id"])
            return []
        r.raise_for_status()
        data = r.json()
        # /v2/tee-times returns an array of facility objects; we asked for one.
        if not isinstance(data, list):
            return []
        slots: list[dict] = []
        for facility_obj in data:
            slots.extend(facility_obj.get("teetimes") or [])
        return slots

    def to_teetime(self, unit: dict, raw_slot: dict) -> Optional["TeeTime"]:
        """Convert one Kenna `teetime` row into our canonical TeeTime."""
        utc_str = raw_slot.get("teetime")
        if not utc_str:
            return None
        try:
            # "2026-05-30T20:45:00.000Z" → tz-aware UTC → local
            utc_dt = dt.datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
            local_dt = utc_dt.astimezone(ZoneInfo(unit.get("timezone", "UTC")))
        except (ValueError, TypeError):
            return None

        rates = raw_slot.get("rates") or []
        # Pick the lowest visible green fee (in cents) as the headline price.
        # Track every rate's info in `classes` for the JSON consumer.
        class_prices: list[ClassPrice] = []
        fees: list[float] = []
        holes = 18
        for rate in rates:
            cents = rate.get("greenFeeCart")
            dollars = (cents / 100.0) if isinstance(cents, (int, float)) else None
            if dollars is not None:
                fees.append(dollars)
            holes = int(rate.get("holes") or holes)
            class_prices.append(ClassPrice(
                booking_class_id=int(rate.get("_id") or 0),
                class_label=rate.get("name") or "default",
                # Kenna doesn't tag residency in the public response; tag "any".
                residency="any",
                green_fee=dollars,
            ))
        price = min(fees) if fees else None

        # `maxPlayers` is the number of additional players this slot can take.
        try:
            spots = int(raw_slot.get("maxPlayers") or 0)
        except (TypeError, ValueError):
            spots = 0

        return TeeTime(
            course_key=unit["course_key"],
            course_name=unit["course_name"],
            date=local_dt.date().isoformat(),
            time=local_dt.strftime("%H:%M"),
            price=price,
            resident_price=None,
            non_resident_price=price,  # Kenna's public rate is non-resident
            available_spots=spots,
            holes=holes,
            booking_url=unit["booking_url"],
            teesheet_id=None,
            schedule_name=unit["course_name"],
            classes=class_prices,
            raw=[raw_slot],
        )


def scrape_kenna(date_iso: str) -> tuple[list["TeeTime"], list[str]]:
    """Run every Kenna scrape unit for one date. Returns (teetimes, errors)."""
    scraper = KennaScraper()
    out: list[TeeTime] = []
    errors: list[str] = []
    for unit in KENNA_UNITS:
        label = (f"[{date_iso}] kenna facility={unit['facility_id']} "
                 f"({unit['course_name']})")
        try:
            raw_slots = scraper.fetch_one(unit, date_iso)
            for raw in raw_slots:
                t = scraper.to_teetime(unit, raw)
                if t is not None:
                    out.append(t)
            log.debug("  %s → %d slots", label, len(raw_slots))
        except Exception as e:
            msg = f"{label}: {e}"
            log.error("  ✗ %s", msg)
            errors.append(msg)
        time.sleep(INTER_REQUEST_DELAY)
    return out, errors


# ─────────────────────────────────────────────────────────────────────────────
# Aggregation
# ─────────────────────────────────────────────────────────────────────────────

def _derive_course_key(schedule_name: str, teesheet_id: int) -> Optional[str]:
    """Map a ForeUp schedule_name + teesheet_id to the dashboard's course_key."""
    n = (schedule_name or "").lower()
    if "torrey" in n:
        if "north" in n:
            return "torrey_north"
        if "south" in n:
            return "torrey_south"
        # Fallback if schedule_name is missing or wonky. Verified 2026-05-13 by
        # inspecting the Torrey Pines booking page (foreupsoftware.com/booking/19347):
        #   teesheet 1487 → "Torrey Pines South" (premium, ~$300+ non-resident)
        #   teesheet 1468 → "Torrey Pines North"
        return "torrey_south" if teesheet_id == 1487 else "torrey_north"
    if "balboa" in n:
        return "balboa"
    if "mission bay" in n:
        return "mission_bay"
    if "coronado" in n:
        return "coronado"
    return None


def _iso_to_us(iso: str) -> str:
    """2026-05-14 → 05-14-2026"""
    y, m, d = iso.split("-")
    return f"{m}-{d}-{y}"


def _parse_row(unit: dict, row: dict) -> Optional[dict]:
    """Pull the fields we care about out of one API row. Returns None on bad rows."""
    time_str = row.get("time")  # "2026-05-14 13:30"
    if not isinstance(time_str, str) or " " not in time_str:
        return None
    date_part, hm = time_str.split(" ", 1)
    if len(hm) < 5:
        return None
    hh_mm = hm[:5]

    green_fee = row.get("green_fee")
    try:
        green_fee = float(green_fee) if green_fee not in (None, False, "") else None
    except (TypeError, ValueError):
        green_fee = None

    try:
        spots = int(row.get("available_spots") or 0)
    except (TypeError, ValueError):
        spots = 0

    try:
        holes_n = int(row.get("holes") or row.get("teesheet_holes") or 18)
    except (TypeError, ValueError):
        holes_n = 18

    teesheet_id = row.get("teesheet_id") or unit["schedule_id"]
    try:
        teesheet_id = int(teesheet_id)
    except (TypeError, ValueError):
        teesheet_id = unit["schedule_id"]

    schedule_name = row.get("schedule_name") or row.get("course_name") or ""
    course_name = row.get("course_name") or schedule_name
    course_key = _derive_course_key(schedule_name, teesheet_id)
    if not course_key:
        return None

    return {
        "course_key": course_key,
        "course_name": course_name,
        "schedule_name": schedule_name,
        "teesheet_id": teesheet_id,
        "date": date_part,
        "time": hh_mm,
        "green_fee": green_fee,
        "spots": spots,
        "holes": holes_n,
    }


def _aggregate(rows_with_unit: list[tuple[dict, dict]]) -> list[TeeTime]:
    """
    Merge rows across booking classes. A slot may appear in multiple classes
    (Resident + Non-Resident); we emit one TeeTime per (course_key, time)
    and attach each class's price separately.
    """
    by_key: dict[tuple[str, str], TeeTime] = {}

    for unit, row in rows_with_unit:
        parsed = _parse_row(unit, row)
        if not parsed:
            continue

        key = (parsed["course_key"], parsed["date"] + "T" + parsed["time"])
        slot = by_key.get(key)
        if slot is None:
            booking_url = (
                f"{FOREUP_BASE}/index.php/booking/{unit['course_id']}"
                f"/{unit['schedule_id']}#/teetimes"
            )
            slot = TeeTime(
                course_key=parsed["course_key"],
                course_name=parsed["course_name"],
                date=parsed["date"],
                time=parsed["time"],
                price=parsed["green_fee"],
                resident_price=None,
                non_resident_price=None,
                available_spots=parsed["spots"],
                holes=parsed["holes"],
                booking_url=booking_url,
                teesheet_id=parsed["teesheet_id"],
                schedule_name=parsed["schedule_name"],
            )
            by_key[key] = slot
        else:
            slot.available_spots = max(slot.available_spots, parsed["spots"])

        residency = unit.get("residency", "any")
        gf = parsed["green_fee"]
        slot.classes.append(ClassPrice(
            booking_class_id=unit["booking_class_id"],
            class_label=unit["class_label"],
            residency=residency,
            green_fee=gf,
        ))
        slot.raw.append(row)

        if gf is not None:
            if residency == "resident":
                if slot.resident_price is None or gf < slot.resident_price:
                    slot.resident_price = gf
            elif residency == "non_resident":
                if slot.non_resident_price is None or gf < slot.non_resident_price:
                    slot.non_resident_price = gf

    # Pick a "headline" price per slot.
    # - Prefer non-resident (what a stranger actually pays — honest default for the dashboard)
    # - Else use the lowest green_fee observed across classes
    for slot in by_key.values():
        if slot.non_resident_price is not None:
            slot.price = slot.non_resident_price
        else:
            fees = [c.green_fee for c in slot.classes if c.green_fee is not None]
            slot.price = min(fees) if fees else None

    return sorted(by_key.values(), key=lambda t: (t.date, t.time, t.course_name))


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────

def scrape_all(dates_iso: list[str], players: int, holes: int = 18) -> ScrapeResult:
    """
    Hit every vendor's scrape units for each date and return one merged
    result. ForeUp (city munis) and Kenna (Coronado) run sequentially per
    date. Per-unit chatter logs at DEBUG; per-date summary at INFO.
    """
    log.info("Scraping %d ForeUp units + %d Kenna units × %d date(s) "
             "(%d players, %d holes)",
             len(SCRAPE_UNITS), len(KENNA_UNITS), len(dates_iso), players, holes)

    foreup = ForeUpScraper()
    foreup_rows: list[tuple[dict, dict]] = []
    kenna_teetimes: list[TeeTime] = []
    errors: list[str] = []

    for date_iso in dates_iso:
        date_rows = 0
        date_errors = 0

        # ── ForeUp pass ─────────────────────────────────────────────────
        for unit in SCRAPE_UNITS:
            label = (f"[{date_iso}] foreup course={unit['course_id']} "
                     f"ts={unit['schedule_id']} cls={unit['booking_class_id']} "
                     f"({unit['class_label']})")
            try:
                rows = foreup.fetch(unit, date_iso, players, holes)
                log.debug("  %s → %d slots", label, len(rows))
                for r in rows:
                    foreup_rows.append((unit, r))
                date_rows += len(rows)
            except Exception as e:
                msg = f"{label}: {e}"
                log.error("  ✗ %s", msg)
                errors.append(msg)
                date_errors += 1
            time.sleep(INTER_REQUEST_DELAY)

        # ── Kenna pass (Coronado) ───────────────────────────────────────
        kenna_for_date, kenna_errs = scrape_kenna(date_iso)
        kenna_teetimes.extend(kenna_for_date)
        errors.extend(kenna_errs)
        date_rows += len(kenna_for_date)
        date_errors += len(kenna_errs)

        log.info("  %s → %d raw slots, %d error(s)", date_iso, date_rows, date_errors)

    # ForeUp slots need _aggregate to dedupe across booking classes; Kenna
    # already emits one TeeTime per slot.
    foreup_teetimes = _aggregate(foreup_rows)

    # Dedupe across vendors by (course_key, date, time) just in case the
    # same slot somehow showed up in both feeds.
    seen: set[tuple] = set()
    merged: list[TeeTime] = []
    for t in foreup_teetimes + kenna_teetimes:
        k = (t.course_key, t.date, t.time)
        if k in seen:
            continue
        seen.add(k)
        merged.append(t)

    merged.sort(key=lambda t: (t.date, t.time, t.course_name))

    log.info("Aggregated %d unique slots (%d ForeUp + %d Kenna) across %d date(s)",
             len(merged), len(foreup_teetimes), len(kenna_teetimes), len(dates_iso))

    return ScrapeResult(
        date=dates_iso[0] if dates_iso else "",
        dates_scraped=list(dates_iso),
        players=players,
        scraped_at=dt.datetime.now().isoformat(timespec="seconds"),
        tee_times=merged,
        errors=errors,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────────────────────────────────────

def print_table(result: ScrapeResult) -> None:
    if not result.tee_times:
        print("\nNo tee times found.\n")
        if result.errors:
            print("Errors:")
            for e in result.errors:
                print(f"  · {e}")
        return

    by_course: dict[str, list[TeeTime]] = {}
    for t in result.tee_times:
        by_course.setdefault(t.course_name, []).append(t)

    dates_label = (result.dates_scraped[0] if len(result.dates_scraped) == 1
                   else f"{result.dates_scraped[0]} … {result.dates_scraped[-1]} "
                        f"({len(result.dates_scraped)} dates)")
    print(f"\n  San Diego Muni Tee Times — {dates_label}")
    print(f"  Scraped: {result.scraped_at}   Players: {result.players}\n")
    print(f"  {'DATE':<11}{'TIME':<8}{'COURSE':<32}{'PRICE':>8}{'RES':>6}{'NONRES':>8}{'SPOTS':>7}")
    print(f"  {'─'*10:<11}{'─'*7:<8}{'─'*31:<32}{'─'*7:>8}{'─'*5:>6}{'─'*7:>8}{'─'*5:>7}")
    for t in result.tee_times:
        h, m = t.time.split(":")
        hh = int(h)
        ampm = "AM" if hh < 12 else "PM"
        h12 = hh if 1 <= hh <= 12 else (hh - 12 if hh > 12 else 12)
        time_disp = f"{h12}:{m} {ampm}"
        price_disp = f"${t.price:.0f}" if t.price is not None else "—"
        res_disp = f"${t.resident_price:.0f}" if t.resident_price is not None else "—"
        nr_disp = f"${t.non_resident_price:.0f}" if t.non_resident_price is not None else "—"
        print(f"  {t.date:<11}{time_disp:<8}{t.course_name[:31]:<32}"
              f"{price_disp:>8}{res_disp:>6}{nr_disp:>8}{t.available_spots:>7}")

    print(f"\n  Total: {len(result.tee_times)} open slots across {len(by_course)} courses.")
    if result.errors:
        print("\n  Errors:")
        for e in result.errors:
            print(f"    · {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Scrape San Diego city muni tee times (Torrey Pines, Balboa, Mission Bay).",
    )
    parser.add_argument("--date", default=None,
                        help="Single ISO date YYYY-MM-DD (default: tomorrow). "
                             "Ignored if --dates or --days is given.")
    parser.add_argument("--dates", default=None,
                        help="Comma-separated list of ISO dates to scrape.")
    parser.add_argument("--days", type=int, default=None,
                        help="Convenience: scrape this many consecutive days "
                             "starting tomorrow (e.g. --days 14).")
    parser.add_argument("--players", type=int, default=4,
                        help="Number of players (default: 4)")
    parser.add_argument("--holes", type=int, default=18, choices=[9, 18],
                        help="9 or 18 holes (default: 18)")
    parser.add_argument("--json", action="store_true",
                        help="Output JSON instead of a table")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    # Resolve target dates. Precedence: --dates > --days > --date > default(tomorrow).
    if args.dates:
        dates_iso = [d.strip() for d in args.dates.split(",") if d.strip()]
    elif args.days:
        start = dt.date.today() + dt.timedelta(days=1)
        dates_iso = [(start + dt.timedelta(days=i)).isoformat()
                     for i in range(args.days)]
    elif args.date:
        dates_iso = [args.date]
    else:
        dates_iso = [(dt.date.today() + dt.timedelta(days=1)).isoformat()]

    try:
        result = scrape_all(dates_iso, args.players, args.holes)
    except KeyboardInterrupt:
        log.warning("Interrupted.")
        sys.exit(130)

    if args.json:
        print(result.to_json())
    else:
        print_table(result)

    sys.exit(0 if result.tee_times else 1)


if __name__ == "__main__":
    main()
