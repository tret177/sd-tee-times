# Inspecting the live ForeUp endpoint

The scraper hits the City of San Diego's online booking system, which runs on
**ForeUp Software** (foreupsoftware.com). If something stops working — the
booking class IDs change, a course moves to a different teesheet, ForeUp adds
a new auth requirement — here's how to find what changed in about five
minutes.

## What "broken" usually looks like

- The scraper logs `HTTP 401/403` for booking classes it expects to be public.
- A course returns `0 slots` across every date you try.
- The endpoint path stops returning JSON.

## Step 1 — confirm the booking page still loads

Visit each course's booking page in a normal browser:

- Mission Bay:  https://foreupsoftware.com/index.php/booking/19346
- Torrey Pines: https://foreupsoftware.com/index.php/booking/19347
- Balboa Park:  https://foreupsoftware.com/index.php/booking/19348

If a page returns 404 or redirects somewhere unexpected, the `course_id` in
`SCRAPE_UNITS` has changed. The new ID is in the URL the city links to from
https://www.sandiego.gov/park-and-recreation/golf.

## Step 2 — re-inspect the embedded booking_classes JSON

ForeUp's booking pages embed a JSON catalog of every booking class for each
teesheet. Each entry has these fields:

```json
{
  "booking_class_id": "3088",
  "teesheet_id": "1487",
  "active": "1",
  "hidden": "0",
  "name": "Non Resident (0 - 90 Days)",
  "days_in_booking_window": "90",
  ...
}
```

To extract them:

1. Open the booking page (e.g. 19347 for Torrey Pines).
2. View page source (⌘+U / Ctrl+U).
3. Search for `"booking_classes":[`. There may be more than one block — Torrey
   has two (one per teesheet, North and South).
4. Each block is an array of class records. Look for `name`, `active=1`,
   `hidden=0` to find the public/bookable classes.
5. Update `SCRAPE_UNITS` in `scraper.py` with the new
   `booking_class_id` + `teesheet_id` pairs you care about.

The relevant classes are typically:

- **Standard / Resident (0-7 days)** — the day-of-week window
- **Advance / (Non-)Resident (8-90 days)** — further-out bookings
- **Non Resident (0-90 days)** — what the public sees
- Skip **Junior**, **Resident Back 9**, **Golf Instructor** (specialty/restricted)

## Step 3 — verify with curl / the scraper

Pick one combo and hit it directly:

```bash
curl -s 'https://foreupsoftware.com/index.php/api/booking/times?time=all&date=MM-DD-YYYY&holes=18&players=4&schedule_id=TEESHEET_ID&booking_class=CLASS_ID&course_id=COURSE_ID' \
  -H 'X-Requested-With: XMLHttpRequest' \
  -H 'X-FU-Golfer-Location: foreup' \
  -H 'Accept: application/json' \
  -H 'Referer: https://foreupsoftware.com/index.php/booking/COURSE_ID'
```

What the response means:

- `[]` — endpoint works, no slots available for that class on that date.
  Often expected (course is full, or the date is outside the class's
  booking window).
- `[{...}, ...]` — working, real data. The fields the scraper uses are
  `time`, `green_fee`, `available_spots`, `schedule_name`, `teesheet_id`.
- `{"status":false, "error":"Invalid API Key."}` — the endpoint is now
  API-key-gated. Try a different path.
- `401 / 403` — the booking class requires authentication (resident login).
  Expected for Torrey Pines Resident classes.

## Step 4 — date format gotcha

ForeUp wants `MM-DD-YYYY`, **not** ISO. The scraper converts internally;
if you're testing the endpoint by hand, remember to convert.

## What's NOT this scraper's job

- **Resident pricing for Torrey Pines.** Those classes require a logged-in
  Resident ID card. The scraper soft-skips 401s on those calls. If you want
  resident pricing, you'd need to add a login flow.
- **Non-Resident bookings of Torrey Pines North.** Non-residents can book
  the South course online but the North non-resident class consistently
  returns 0 rows — the city appears to restrict non-resident North bookings
  to walk-up/phone. Not something we can scrape.

## Legal / TOS note

`INTER_REQUEST_DELAY` at the top of `scraper.py` controls how polite this
script is. Keep it at ≥1 second. Hobbyist personal use is fine; aggressive
polling or commercial use isn't. Read the city's terms before deploying
this on a schedule.
