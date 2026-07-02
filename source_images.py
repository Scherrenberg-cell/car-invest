#!/usr/bin/env python3
"""
Carnomics Image Sourcer
========================
Finds, validates, and attaches a real front-3/4 photo to each car in the
Carnomics app database (carnomics_app.html).

WHY THIS EXISTS
---------------
Doing this one car at a time in chat requires a search + a fetch + a human
eyeballing the result for every single one of ~1,240 cars. This script does
all three automatically:

  1. SOURCE   — Wikimedia Commons API by default (free, CC-licensed, legal to
                embed). For cars on the replica-risk list (Cobra, GT40,
                Countach, etc.) it instead searches trusted auction/dealer
                sites and pulls the real photo via the page's `og:image` tag
                (every major listing site — RM Sotheby's, Bonhams, Classic
                Trader, Car & Classic, Collecting Cars — sets this).
  2. VALIDATE — Sends each candidate photo to Claude (vision) along with the
                brand/model/years and asks it to confirm the photo is
                genuinely that car, not a different generation, a replica,
                an engine/interior shot, etc. This replaces "cross-check
                against Google Images" with something both automatable and
                more reliable: a model that actually looks at the photo.
  3. APPLY    — Writes the validated (url, credit) pair straight into the
                DB.rows array inside carnomics_app.html, in the `image` /
                `imageCredit` columns already added to the schema.

Anything that fails sourcing or fails validation is logged to a CSV for
manual follow-up rather than silently skipped or guessed at.

REQUIREMENTS
------------
    pip install requests beautifulsoup4 anthropic

    export ANTHROPIC_API_KEY=sk-ant-...        # for the validation step
    (Sourcing works without a key; validation is skipped with a warning
     if the key isn't set — candidates are still logged for manual review.)

USAGE
-----
    # Dry run on the next 20 un-imaged cars, Tier A first, no API changes:
    python3 source_images.py --input carnomics_app.html --limit 20 --dry-run

    # Real run, apply validated results directly into the app file:
    python3 source_images.py --input carnomics_app.html --output carnomics_app.html --apply

    # Only replica-risk cars, using the auction-listing path:
    python3 source_images.py --input carnomics_app.html --only-replica-risk --apply

    # Everything, unattended, in batches of 50 with a 1200-row cap:
    python3 source_images.py --input carnomics_app.html --output carnomics_app.html \\
        --apply --batch-size 50 --max-rows 1200

OUTPUT
------
    - Patched carnomics_app.html (if --apply)
    - image_sourcing_report.csv — every car attempted, source tried, URL,
      validation verdict, and reason. This is your audit trail.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import base64
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "CarnomicsImageSourcer/1.0 (internal tooling; contact: owner)"
REQUEST_TIMEOUT = 15
POLITE_DELAY = 0.5  # seconds between external requests

# Cars with a large aftermarket replica/kit-car/continuation culture —
# route these to auction/dealer listings, never Commons category search.
REPLICA_RISK_KEYWORDS = [
    "ac cobra", "ac ace", "ac aceca", "ford gt40",
    "jaguar d-type", "jaguar c-type", "jaguar e-type lightweight", "jaguar xkss",
    "lamborghini countach",
    "porsche 356", "porsche 550",
    "aston martin db5", "aston martin db4 gt zagato",
    "lotus seven", "lotus mk vi",
    "shelby daytona",
    "ferrari 250 gto", "ferrari 250 gt california", "ferrari dino", "ferrari 288 gto",
    "bugatti eb110",
    "mg td", "mg tc",
    "excalibur",
    "de tomaso pantera",
    "mercedes-benz w198 300sl", "mercedes-benz 300sl",
    "bentley blower", "bentley speed six",
]

TRUSTED_LISTING_SITES = [
    "rmsothebys.com", "bonhams.com", "cars.bonhams.com", "carsonline.bonhams.com",
    "classic-trader.com", "carandclassic.com", "classic.com",
    "collectingcars.com", "goodingco.com", "classicdriver.com",
]


def is_replica_risk(brand, model):
    text = f"{brand} {model}".lower()
    return any(kw in text for kw in REPLICA_RISK_KEYWORDS)


# ---------------------------------------------------------------------------
# Step 1a: Wikimedia Commons sourcing
# ---------------------------------------------------------------------------

def commons_search(brand, model, years):
    """Search Wikimedia Commons for a category or file matching the car.
    Returns a list of candidate dicts: {url, title, width, height}."""
    query = f"{brand} {model}"
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrnamespace": 6,  # File namespace
        "gsrsearch": f"{query} filetype:bitmap",
        "gsrlimit": 10,
        "prop": "imageinfo",
        "iiprop": "url|size|mime",
        "iiurlwidth": 1200,
    }
    try:
        r = requests.get(COMMONS_API, params=params, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"    [commons] search failed: {e}", file=sys.stderr)
        return []

    pages = data.get("query", {}).get("pages", {})
    candidates = []
    for page in pages.values():
        title = page.get("title", "")
        for info in page.get("imageinfo", []):
            url = info.get("thumburl") or info.get("url")
            mime = info.get("mime", "")
            if not url or not mime.startswith("image/"):
                continue
            # Skip obvious non-photo files (drawings, logos, diagrams)
            lowered = title.lower()
            if any(bad in lowered for bad in ["logo", "diagram", "drawing", "badge.jpg", "emblem"]):
                continue
            candidates.append({
                "url": url,
                "title": title,
                "width": info.get("width"),
                "height": info.get("height"),
            })
    # Prefer filenames that hint at a front 3/4 angle
    def angle_score(c):
        t = c["title"].lower()
        for kw in ["fvr", "front right", "front-right", "3/4 front", "front 3/4", "front view right"]:
            if kw in t:
                return 0
        for kw in ["front"]:
            if kw in t:
                return 1
        for kw in ["engine", "interior", "dashboard", "rear", "badge"]:
            if kw in t:
                return 3
        return 2

    candidates.sort(key=angle_score)
    return candidates[:5]


# ---------------------------------------------------------------------------
# Step 1b: Auction/dealer listing sourcing (for replica-risk cars)
# ---------------------------------------------------------------------------

def web_search_duckduckgo(query, max_results=6):
    """Lightweight, keyless web search via DuckDuckGo's HTML endpoint.
    NOTE: this scrapes a public HTML page rather than calling a stable API —
    acceptable for occasional internal tooling use, but if you run this at
    high volume, swap in a proper search API (Bing Web Search, SerpApi,
    Google Custom Search) using your own API key instead."""
    try:
        r = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
    except Exception as e:
        print(f"    [search] failed: {e}", file=sys.stderr)
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    results = []
    for a in soup.select("a.result__a")[:max_results]:
        href = a.get("href")
        if href:
            results.append(href)
    return results


def extract_og_image(url):
    """Fetch a listing page and pull its og:image meta tag — the real photo
    URL almost every auction/dealer site exposes for social-share previews."""
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        print(f"    [fetch] {url} failed: {e}", file=sys.stderr)
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    tag = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "og:image"})
    if tag and tag.get("content"):
        return tag["content"]
    return None


def auction_listing_search(brand, model, years):
    """Search trusted listing sites for this car and extract a real photo URL."""
    candidates = []
    query = f'"{brand} {model}" for sale (site:rmsothebys.com OR site:bonhams.com OR site:classic-trader.com OR site:carandclassic.com OR site:collectingcars.com)'
    urls = web_search_duckduckgo(query)
    for url in urls:
        if not any(site in url for site in TRUSTED_LISTING_SITES):
            continue
        time.sleep(POLITE_DELAY)
        img = extract_og_image(url)
        if img:
            candidates.append({"url": img, "title": url, "source_page": url})
        if len(candidates) >= 3:
            break
    return candidates


# ---------------------------------------------------------------------------
# Step 2: Claude vision validation
# ---------------------------------------------------------------------------

def validate_with_claude(candidate_url, brand, model, years, api_key):
    """Downloads the candidate image and asks Claude to confirm it genuinely
    shows the specified car. Returns (is_valid: bool, reason: str)."""
    if not api_key:
        return None, "No ANTHROPIC_API_KEY set — skipped, needs manual review"

    try:
        img_resp = requests.get(candidate_url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
        img_resp.raise_for_status()
        content_type = img_resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
        if not content_type.startswith("image/"):
            return False, f"URL did not return an image (got {content_type})"
        img_b64 = base64.b64encode(img_resp.content).decode("utf-8")
    except Exception as e:
        return False, f"Could not download candidate image: {e}"

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": content_type, "data": img_b64}},
                    {"type": "text", "text": (
                        f"Is this photo genuinely a {brand} {model} ({years})? "
                        f"Check specifically: correct make and model (not a similarly-named "
                        f"different model, not a replica/kit-car, not a modified/tuned version "
                        f"unless the model name itself implies that trim), and that it's an "
                        f"actual photo of a car (not an engine bay, interior, diagram, or logo). "
                        f"Reply with exactly one line: YES or NO, then a dash, then a short reason. "
                        f"Example: 'YES - correct model and generation, clean exterior shot' or "
                        f"'NO - this appears to be a later facelift model, not the specified years'."
                    )},
                ],
            }],
        )
        text = message.content[0].text.strip()
        is_valid = text.upper().startswith("YES")
        return is_valid, text
    except Exception as e:
        return False, f"Validation call failed: {e}"


# ---------------------------------------------------------------------------
# DB read / write helpers
# ---------------------------------------------------------------------------

def load_db(html_path):
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    m = re.search(r"const DB=(\{.*?\});", content, re.DOTALL)
    if not m:
        raise RuntimeError("Could not find `const DB={...};` in the input file.")
    data = json.loads(m.group(1))
    return content, data, m.start(), m.end()


def save_db(content, data, start, end, output_path):
    new_json = json.dumps(data, ensure_ascii=False)
    new_content = content[:start] + "const DB=" + new_json + ";" + content[end:]
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(new_content)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True, help="Path to carnomics_app.html")
    ap.add_argument("--output", default=None, help="Where to write the patched file (default: overwrite input if --apply)")
    ap.add_argument("--report", default="image_sourcing_report.csv", help="CSV audit log path")
    ap.add_argument("--apply", action="store_true", help="Write validated results back into the DB. Without this flag, it's a dry run.")
    ap.add_argument("--limit", type=int, default=None, help="Max number of cars to process this run")
    ap.add_argument("--max-rows", type=int, default=None, help="Alias for --limit")
    ap.add_argument("--batch-size", type=int, default=None, help="Process in batches, pausing between (for very large runs)")
    ap.add_argument("--tier", default=None, help="Filter to a specific Investment Tier substring, e.g. 'Solid Collector'")
    ap.add_argument("--only-replica-risk", action="store_true", help="Only process cars on the replica-risk list")
    ap.add_argument("--skip-replica-risk", action="store_true", help="Skip replica-risk cars entirely")
    ap.add_argument("--skip-existing", action="store_true", default=True, help="Skip cars that already have an image (default: on)")
    args = ap.parse_args()

    limit = args.limit or args.max_rows
    output_path = args.output or (args.input if args.apply else None)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("WARNING: ANTHROPIC_API_KEY not set — sourcing will run but nothing will be", file=sys.stderr)
        print("         auto-validated or auto-applied. Candidates will be logged for manual review.\n", file=sys.stderr)

    content, data, start, end = load_db(args.input)
    rows = data["rows"]
    print(f"Loaded {len(rows)} cars from {args.input}")

    # F column indices — keep in sync with the app's own F map
    F = {"brand": 0, "model": 1, "years": 2, "tier": 15, "image": 29, "imageCredit": 30}

    targets = []
    for i, row in enumerate(rows):
        brand, model, years = row[F["brand"]], row[F["model"]], row[F["years"]]
        tier = row[F["tier"]] if len(row) > F["tier"] else ""
        if args.skip_existing and len(row) > F["image"] and row[F["image"]]:
            continue
        if args.tier and args.tier.lower() not in str(tier).lower():
            continue
        risky = is_replica_risk(brand, model)
        if args.only_replica_risk and not risky:
            continue
        if args.skip_replica_risk and risky:
            continue
        targets.append(i)

    if limit:
        targets = targets[:limit]

    print(f"{len(targets)} cars queued for this run "
          f"({'DRY RUN — no changes will be applied' if not args.apply else 'will write to ' + str(output_path)})\n")

    report_rows = []
    applied = 0

    def process_batch(batch):
        nonlocal applied
        for i in batch:
            row = rows[i]
            brand, model, years = row[F["brand"]], row[F["model"]], row[F["years"]]
            risky = is_replica_risk(brand, model)
            print(f"[{i}] {brand} {model} ({years}) {'[REPLICA-RISK -> auction search]' if risky else '[Commons]'}")

            if risky:
                candidates = auction_listing_search(brand, model, years)
            else:
                candidates = commons_search(brand, model, years)
                time.sleep(POLITE_DELAY)

            if not candidates:
                print("    no candidates found")
                report_rows.append([brand, model, years, "", "", "NO_CANDIDATES", ""])
                continue

            chosen = None
            reason = ""
            for c in candidates:
                url = c["url"]
                is_valid, verdict = validate_with_claude(url, brand, model, years, api_key)
                time.sleep(POLITE_DELAY)
                if is_valid:
                    chosen = c
                    reason = verdict
                    break
                elif is_valid is None:
                    # No API key — log the top candidate for manual review, don't apply
                    report_rows.append([brand, model, years, url, c.get("title", ""), "NEEDS_MANUAL_REVIEW", verdict])
                    break
                else:
                    report_rows.append([brand, model, years, url, c.get("title", ""), "REJECTED", verdict])

            if chosen:
                credit = f"Source: {'Auction/dealer listing' if risky else 'Wikimedia Commons'}"
                print(f"    VALIDATED: {chosen['url'][:80]}")
                report_rows.append([brand, model, years, chosen["url"], chosen.get("title", ""), "VALIDATED", reason])
                if args.apply:
                    row[F["image"]] = chosen["url"]
                    row[F["imageCredit"]] = credit
                    applied += 1
            elif not any(r[0] == brand and r[1] == model and r[5] == "VALIDATED" for r in report_rows):
                print("    no candidate passed validation")

    if args.batch_size:
        for b_start in range(0, len(targets), args.batch_size):
            batch = targets[b_start:b_start + args.batch_size]
            print(f"\n--- Batch {b_start // args.batch_size + 1} ({len(batch)} cars) ---")
            process_batch(batch)
    else:
        process_batch(targets)

    with open(args.report, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Brand", "Model", "Years", "Candidate URL", "Candidate Title", "Status", "Reason"])
        writer.writerows(report_rows)
    print(f"\nReport written to {args.report}")

    if args.apply and output_path:
        save_db(content, data, start, end, output_path)
        print(f"Applied {applied} validated images -> {output_path}")
    elif not args.apply:
        print(f"\nDRY RUN complete — {applied} would have been applied. Re-run with --apply to write changes.")


if __name__ == "__main__":
    main()
