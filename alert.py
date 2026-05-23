"""
Alert engine for the SD tee time scraper.

Reads `alerts.json` (config: ntfy topic + list of "watchers", each with
match criteria), compares the current `times.json` against the previous
scrape (`.times.prev.json`), and posts a push notification for any slot
that matches a watcher's criteria now but did NOT match in the previous
scrape.

Invoked by refresh.sh after a successful scrape. Designed to be silent
on the happy path (no new matching slots = no output, exit 0). Will skip
alerting on the very first run (no baseline to diff against) so you don't
get a flood of "matching" slots that have been there all along.

CLI:
    python alert.py                       # normal run, posts to ntfy
    python alert.py --dry-run             # print what WOULD be sent, don't POST
    python alert.py --verbose             # show debug logs
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path
from typing import Optional

import requests


HERE = Path(__file__).parent.resolve()
DEFAULT_CONFIG = HERE / "alerts.json"
DEFAULT_TIMES = HERE / "times.json"
DEFAULT_PREV = HERE / ".times.prev.json"

WEEKDAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

log = logging.getLogger("alerts")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)


# ─────────────────────────────────────────────────────────────────────────────
# Matching
# ─────────────────────────────────────────────────────────────────────────────

def _slot_id(t: dict) -> tuple:
    """Stable identity for a tee-time slot across scrapes."""
    return (t.get("course_key"), t.get("date"), t.get("time"))


def _matches(t: dict, watcher: dict) -> bool:
    """
    Does this slot satisfy every constraint declared on this watcher?
    Day-of-week is derived from the slot's own date (t["date"]), so this
    works correctly for multi-date snapshots.
    """
    course_keys = watcher.get("course_keys")
    if course_keys and t.get("course_key") not in course_keys:
        return False

    price = t.get("price")
    if "max_price" in watcher:
        if price is None or price > watcher["max_price"]:
            return False
    if "min_price" in watcher:
        if price is None or price < watcher["min_price"]:
            return False

    if "min_spots" in watcher and (t.get("available_spots") or 0) < watcher["min_spots"]:
        return False

    tt = t.get("time", "")
    if "earliest_time" in watcher and tt < watcher["earliest_time"]:
        return False
    if "latest_time" in watcher and tt > watcher["latest_time"]:
        return False

    dow = watcher.get("days_of_week")
    if dow:
        try:
            wd = WEEKDAY_ABBR[dt.datetime.strptime(t.get("date", ""), "%Y-%m-%d").weekday()]
        except ValueError:
            return False
        if wd not in dow:
            return False

    return True


# ─────────────────────────────────────────────────────────────────────────────
# Formatting
# ─────────────────────────────────────────────────────────────────────────────

def _format_slot(t: dict, include_date: bool = False) -> str:
    """One-line display, e.g. 'Sat May 24  8:24 AM  Balboa Park - $34 - 4 spots'"""
    hh, mm = t.get("time", "00:00").split(":")
    h = int(hh)
    m = int(mm)
    ampm = "AM" if h < 12 else "PM"
    h12 = h if 1 <= h <= 12 else (h - 12 if h > 12 else 12)
    price = t.get("price")
    price_disp = f"${int(price)}" if price is not None else "—"
    spots = t.get("available_spots", "?")
    name = t.get("course_name") or t.get("course_key") or "?"

    if include_date:
        try:
            dobj = dt.datetime.strptime(t.get("date", ""), "%Y-%m-%d").date()
            date_disp = dobj.strftime("%a %b %d")  # "Sat May 24"
        except ValueError:
            date_disp = t.get("date", "?")
        return f"{date_disp}  {h12}:{m:02d} {ampm}  {name} - {price_disp} - {spots} spots"
    return f"{h12}:{m:02d} {ampm}  {name} - {price_disp} - {spots} spots"


# ─────────────────────────────────────────────────────────────────────────────
# Notifier
# ─────────────────────────────────────────────────────────────────────────────

def _notify_ntfy(server: str, topic: str, title: str, body: str,
                 click_url: Optional[str] = None,
                 priority: str = "default") -> None:
    """POST a message to an ntfy topic."""
    url = f"{server.rstrip('/')}/{topic}"
    headers = {
        "Title": title,
        "Priority": priority,
    }
    if click_url:
        headers["Click"] = click_url
    r = requests.post(url, data=body.encode("utf-8"), headers=headers, timeout=15)
    r.raise_for_status()


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────

def _load_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning("could not parse %s: %s", path, e)
        return None


def run(config: dict, current: dict, previous: Optional[dict], dry_run: bool) -> int:
    """
    For each watcher, find slots that match NOW but did not match in the
    previous scrape (per-date), aggregate them across all dates, and fire
    one notification per watcher.

    Slots are only comparable across snapshots when both snapshots covered
    the same date. Dates in `current` but not `previous` are treated as
    baseline (no alert), so adding a date to the scrape window doesn't
    flood notifications.
    """
    if not current or not current.get("tee_times"):
        log.debug("no tee_times in current snapshot")
        return 0

    # Group current slots by date.
    cur_by_date: dict[str, list[dict]] = {}
    for t in current["tee_times"]:
        cur_by_date.setdefault(t.get("date", ""), []).append(t)

    # Group previous slots by date, but only for dates the previous snapshot
    # actually claimed to scrape. (A date missing from prev's tee_times could
    # mean either "scraped, no slots" or "didn't scrape" — the dates_scraped
    # list disambiguates.)
    prev_by_date: dict[str, list[dict]] = {}
    prev_dates_scraped: set[str] = set()
    if previous:
        prev_dates_scraped = set(previous.get("dates_scraped")
                                 or ([previous["date"]] if previous.get("date") else []))
        for t in previous.get("tee_times", []):
            prev_by_date.setdefault(t.get("date", ""), []).append(t)
        # Ensure every "previously-scraped" date has an entry (possibly empty).
        for d in prev_dates_scraped:
            prev_by_date.setdefault(d, [])

    sent = 0
    for watcher in config.get("watchers", []):
        name = watcher.get("name")
        if not name:
            log.debug("skipping unnamed watcher")
            continue

        all_newly: list[dict] = []
        baseline_skips = 0
        total_cur_matches = 0

        for date_iso, day_cur in cur_by_date.items():
            cur_matches = [t for t in day_cur if _matches(t, watcher)]
            if not cur_matches:
                continue
            total_cur_matches += len(cur_matches)

            if date_iso not in prev_dates_scraped:
                # We didn't have data for this date last time — baseline only.
                baseline_skips += len(cur_matches)
                continue

            prev_ids = {_slot_id(t) for t in prev_by_date.get(date_iso, [])
                        if _matches(t, watcher)}
            day_newly = [t for t in cur_matches if _slot_id(t) not in prev_ids]
            all_newly.extend(day_newly)

        if not total_cur_matches:
            log.debug("watcher %r: no matches", name)
            continue

        if baseline_skips and not all_newly:
            log.info("watcher %r: %d match(es) all on baseline date(s) — "
                     "skipping alert", name, baseline_skips)
            continue

        if not all_newly:
            log.debug("watcher %r: %d match(es), nothing new",
                      name, total_cur_matches)
            continue

        all_newly.sort(key=lambda x: (x.get("date", ""), x.get("time", "")))

        # Are these newly slots spread across multiple dates? If so, show
        # the date on each line and call it out in the title.
        distinct_dates = {t.get("date") for t in all_newly}
        multi_date = len(distinct_dates) > 1
        date_label = (f"across {len(distinct_dates)} dates" if multi_date
                      else next(iter(distinct_dates), "?"))
        try:
            single_date_pretty = dt.datetime.strptime(
                next(iter(distinct_dates)), "%Y-%m-%d"
            ).strftime("%a %b %d")
        except (ValueError, StopIteration):
            single_date_pretty = next(iter(distinct_dates), "?")
        title = (f"{name}: {len(all_newly)} new slot"
                 f"{'s' if len(all_newly) != 1 else ''} "
                 f"{date_label if multi_date else 'on ' + single_date_pretty}")

        lines = [_format_slot(t, include_date=multi_date) for t in all_newly[:10]]
        if len(all_newly) > 10:
            lines.append(f"...and {len(all_newly) - 10} more")
        body = "\n".join(lines)
        click_url = all_newly[0].get("booking_url") or None
        priority = watcher.get("priority", "default")

        log.info("watcher %r: NEW matches: %d (across %d date(s))",
                 name, len(all_newly), len(distinct_dates))

        if dry_run:
            print(f"\n=== {title} ===")
            print(body)
            if click_url:
                print(f"(click -> {click_url})")
            continue

        ntfy = config.get("ntfy", {})
        try:
            _notify_ntfy(
                server=ntfy.get("server", "https://ntfy.sh"),
                topic=ntfy["topic"],
                title=title,
                body=body,
                click_url=click_url,
                priority=priority,
            )
            sent += 1
        except Exception as e:
            log.error("ntfy POST failed for watcher %r: %s", name, e)

    return sent


def main():
    p = argparse.ArgumentParser(description="Tee-time alert engine.")
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--current", default=str(DEFAULT_TIMES))
    p.add_argument("--previous", default=str(DEFAULT_PREV))
    p.add_argument("--dry-run", action="store_true",
                   help="print what would be sent, don't POST")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    config = _load_json(Path(args.config))
    if not config:
        log.debug("no alerts.json — alerts disabled")
        return

    if not config.get("watchers"):
        log.debug("alerts.json has no watchers — nothing to do")
        return

    topic = (config.get("ntfy") or {}).get("topic", "")
    if not topic or topic.upper().startswith("REPLACE"):
        log.warning("ntfy.topic not set in alerts.json — alerts disabled")
        return

    current = _load_json(Path(args.current))
    if not current:
        log.warning("no current times.json at %s — nothing to alert on", args.current)
        return

    previous = _load_json(Path(args.previous))

    n = run(config, current, previous, dry_run=args.dry_run)
    if n:
        log.info("sent %d ntfy notification(s)", n)


if __name__ == "__main__":
    main()
