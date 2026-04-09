#!/usr/bin/env python3
"""
Medical Malpractice Case Scraper Agent

Searches publicly accessible court record databases for recently filed
medical malpractice cases and extracts attorney contact information.

Primary source: CourtListener REST API (Free Law Project)
  - Free, no API key required (but recommended for higher rate limits)
  - Aggregates federal PACER dockets and state court records
  - Docs: https://www.courtlistener.com/help/api/rest/

Secondary source: Direct PACER RSS feeds
  - Federal courts publish free RSS feeds of newly filed cases
  - No login required for RSS; full docket details require PACER account

Output: CSV with columns:
  case_number, case_name, filing_date, court, plaintiff_attorney,
  law_firm, phone, email, source_url
"""

import argparse
import csv
import json
import logging
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

try:
    import requests
except ImportError:
    print(
        "Error: 'requests' library is required.\n"
        "Install it with: pip install requests\n"
        "Or install all dependencies: pip install -r requirements.txt"
    )
    sys.exit(1)

try:
    import feedparser

    HAS_FEEDPARSER = True
except ImportError:
    HAS_FEEDPARSER = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

COURTLISTENER_BASE = "https://www.courtlistener.com/api/rest/v4"
PACER_RSS_FEEDS = [
    # A selection of high-volume federal district court RSS feeds
    "https://ecf.nysd.uscourts.gov/cgi-bin/rss_outside.pl",  # S.D.N.Y.
    "https://ecf.ilnd.uscourts.gov/cgi-bin/rss_outside.pl",  # N.D. Ill.
    "https://ecf.txsd.uscourts.gov/cgi-bin/rss_outside.pl",  # S.D. Tex.
    "https://ecf.cacd.uscourts.gov/cgi-bin/rss_outside.pl",  # C.D. Cal.
    "https://ecf.flsd.uscourts.gov/cgi-bin/rss_outside.pl",  # S.D. Fla.
    "https://ecf.paed.uscourts.gov/cgi-bin/rss_outside.pl",  # E.D. Pa.
    "https://ecf.mad.uscourts.gov/cgi-bin/rss_outside.pl",   # D. Mass.
    "https://ecf.ohnd.uscourts.gov/cgi-bin/rss_outside.pl",  # N.D. Ohio
]

SEARCH_QUERIES = [
    "medical malpractice",
    "medical negligence",
    "surgical error",
    "misdiagnosis",
    "failure to diagnose",
]

CSV_COLUMNS = [
    "case_number",
    "case_name",
    "filing_date",
    "court",
    "plaintiff_attorney",
    "law_firm",
    "phone",
    "email",
    "source_url",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class CaseRecord:
    case_number: str = ""
    case_name: str = ""
    filing_date: str = ""
    court: str = ""
    plaintiff_attorney: str = ""
    law_firm: str = ""
    phone: str = ""
    email: str = ""
    source_url: str = ""


# ---------------------------------------------------------------------------
# CourtListener source
# ---------------------------------------------------------------------------


class CourtListenerSource:
    """Searches the CourtListener API for medical malpractice dockets."""

    def __init__(self, api_token: Optional[str] = None, days_back: int = 90):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "MedMalScraper/1.0 (legal research)"})
        if api_token:
            self.session.headers["Authorization"] = f"Token {api_token}"
        self.days_back = days_back

    def _rate_limit_pause(self):
        """Respectful rate limiting — 1 request/second without token."""
        time.sleep(1.0)

    def search_dockets(self, query: str, max_pages: int = 3) -> list[dict]:
        """Search CourtListener dockets for a given query string."""
        date_after = (datetime.now() - timedelta(days=self.days_back)).strftime("%Y-%m-%d")
        results = []

        for page in range(1, max_pages + 1):
            params = {
                "q": query,
                "type": "r",  # RECAP dockets
                "filed_after": date_after,
                "order_by": "dateFiled desc",
                "page": page,
            }
            url = f"https://www.courtlistener.com/api/rest/v4/search/?{urlencode(params)}"
            log.info(f"CourtListener search: query={query!r}  page={page}")

            try:
                resp = self.session.get(url, timeout=30)
                if resp.status_code == 429:
                    log.warning("Rate limited by CourtListener — pausing 30s")
                    time.sleep(30)
                    continue
                resp.raise_for_status()
            except requests.RequestException as e:
                log.error(f"CourtListener request failed: {e}")
                break

            data = resp.json()
            hits = data.get("results", [])
            if not hits:
                break
            results.extend(hits)
            self._rate_limit_pause()

        return results

    def fetch_docket_detail(self, docket_id: int) -> Optional[dict]:
        """Fetch full docket detail including parties and attorneys."""
        url = f"{COURTLISTENER_BASE}/dockets/{docket_id}/"
        log.info(f"Fetching docket detail: {docket_id}")
        try:
            resp = self.session.get(url, timeout=30)
            if resp.status_code == 429:
                time.sleep(30)
                resp = self.session.get(url, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            log.error(f"Failed to fetch docket {docket_id}: {e}")
            return None
        finally:
            self._rate_limit_pause()

    def fetch_parties(self, docket_id: int) -> list[dict]:
        """Fetch the parties endpoint for a docket to get attorney info."""
        url = f"{COURTLISTENER_BASE}/parties/?docket={docket_id}"
        log.info(f"Fetching parties for docket: {docket_id}")
        all_parties = []
        try:
            while url:
                resp = self.session.get(url, timeout=30)
                if resp.status_code == 429:
                    time.sleep(30)
                    resp = self.session.get(url, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                all_parties.extend(data.get("results", []))
                url = data.get("next")
                self._rate_limit_pause()
        except requests.RequestException as e:
            log.error(f"Failed to fetch parties for docket {docket_id}: {e}")
        return all_parties

    def extract_records(self, max_results: int = 50) -> list[CaseRecord]:
        """Run all queries and extract structured CaseRecords."""
        seen_dockets = set()
        records = []

        for query in SEARCH_QUERIES:
            hits = self.search_dockets(query)
            for hit in hits:
                docket_id = hit.get("docket_id")
                if not docket_id or docket_id in seen_dockets:
                    continue
                seen_dockets.add(docket_id)

                case_name = hit.get("caseName", "") or hit.get("case_name", "")
                case_number = hit.get("docketNumber", "") or hit.get("docket_number", "")
                court = hit.get("court", "") or hit.get("court_citation_string", "")
                filing_date = hit.get("dateFiled", "") or hit.get("date_filed", "")
                source_url = f"https://www.courtlistener.com/docket/{docket_id}/"

                # Try to get attorney info from the search result first
                attorneys_from_hit = self._extract_attorneys_from_hit(hit)
                if attorneys_from_hit:
                    for atty in attorneys_from_hit:
                        rec = CaseRecord(
                            case_number=case_number,
                            case_name=case_name,
                            filing_date=filing_date,
                            court=court,
                            plaintiff_attorney=atty.get("name", ""),
                            law_firm=atty.get("firm", ""),
                            phone=atty.get("phone", ""),
                            email=atty.get("email", ""),
                            source_url=source_url,
                        )
                        records.append(rec)
                else:
                    # Fallback: fetch party detail from the API
                    parties = self.fetch_parties(docket_id)
                    plaintiff_attorneys = self._extract_plaintiff_attorneys(parties)
                    if plaintiff_attorneys:
                        for atty in plaintiff_attorneys:
                            rec = CaseRecord(
                                case_number=case_number,
                                case_name=case_name,
                                filing_date=filing_date,
                                court=court,
                                plaintiff_attorney=atty.get("name", ""),
                                law_firm=atty.get("firm", ""),
                                phone=atty.get("phone", ""),
                                email=atty.get("email", ""),
                                source_url=source_url,
                            )
                            records.append(rec)
                    else:
                        # Record the case even without attorney details
                        records.append(
                            CaseRecord(
                                case_number=case_number,
                                case_name=case_name,
                                filing_date=filing_date,
                                court=court,
                                source_url=source_url,
                            )
                        )

                if len(records) >= max_results:
                    break
            if len(records) >= max_results:
                break

        return records[:max_results]

    def _extract_attorneys_from_hit(self, hit: dict) -> list[dict]:
        """Try to extract attorney info directly from a search hit."""
        attorneys = []
        # CourtListener search results sometimes embed attorney info
        atty_text = hit.get("attorney", "") or ""
        if not atty_text:
            return attorneys

        # The attorney field may contain comma-separated entries
        for entry in atty_text.split("\n"):
            entry = entry.strip()
            if not entry:
                continue
            attorneys.append({"name": entry, "firm": "", "phone": "", "email": ""})
        return attorneys

    def _extract_plaintiff_attorneys(self, parties: list[dict]) -> list[dict]:
        """Extract attorneys representing plaintiffs from party data."""
        attorneys = []
        for party in parties:
            party_type = (party.get("party_type", {}) or {})
            party_type_name = ""
            if isinstance(party_type, dict):
                party_type_name = party_type.get("name", "")
            elif isinstance(party_type, str):
                party_type_name = party_type

            if "plaintiff" not in party_type_name.lower():
                continue

            for atty in party.get("attorneys", []):
                atty_info = {
                    "name": atty.get("attorney_name", "") or atty.get("name", ""),
                    "firm": atty.get("attorney_firm", "") or atty.get("firm_name", ""),
                    "phone": atty.get("phone", ""),
                    "email": atty.get("email", ""),
                }
                # Try to extract contact info from the roles/contact field
                contact = atty.get("contact", "") or atty.get("contact_raw", "") or ""
                if contact:
                    atty_info["phone"] = atty_info["phone"] or self._find_phone(contact)
                    atty_info["email"] = atty_info["email"] or self._find_email(contact)
                attorneys.append(atty_info)
        return attorneys

    @staticmethod
    def _find_phone(text: str) -> str:
        import re
        match = re.search(r"[\(]?\d{3}[\)\-\.\s]?\s*\d{3}[\-\.\s]\d{4}", text)
        return match.group(0).strip() if match else ""

    @staticmethod
    def _find_email(text: str) -> str:
        import re
        match = re.search(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", text)
        return match.group(0).strip() if match else ""


# ---------------------------------------------------------------------------
# PACER RSS source (supplementary)
# ---------------------------------------------------------------------------


class PacerRSSSource:
    """Parses PACER RSS feeds for recently filed medical malpractice cases.

    PACER RSS feeds are free and list the most recent filings per court.
    They don't contain attorney details, but provide case numbers and
    names that can be cross-referenced.
    """

    def __init__(self):
        if not HAS_FEEDPARSER:
            log.warning(
                "feedparser not installed — PACER RSS source disabled. "
                "Install with: pip install feedparser"
            )

    def extract_records(self, max_results: int = 50) -> list[CaseRecord]:
        if not HAS_FEEDPARSER:
            return []

        records = []
        keywords = {"malpractice", "negligence", "medical", "surgical", "misdiagnosis"}

        for feed_url in PACER_RSS_FEEDS:
            log.info(f"Parsing PACER RSS: {feed_url}")
            try:
                feed = feedparser.parse(feed_url)
            except Exception as e:
                log.error(f"Failed to parse RSS {feed_url}: {e}")
                continue

            court_name = feed.feed.get("title", feed_url)
            for entry in feed.entries:
                title = entry.get("title", "").lower()
                summary = entry.get("summary", "").lower()
                combined = title + " " + summary

                if not any(kw in combined for kw in keywords):
                    continue

                # Extract case number from title — typical format "Case 1:24-cv-01234"
                import re
                case_match = re.search(r"\d+:\d+-\w+-\d+", entry.get("title", ""))
                case_number = case_match.group(0) if case_match else ""

                records.append(
                    CaseRecord(
                        case_number=case_number,
                        case_name=entry.get("title", ""),
                        filing_date=entry.get("published", ""),
                        court=court_name,
                        source_url=entry.get("link", ""),
                    )
                )

                if len(records) >= max_results:
                    break
            if len(records) >= max_results:
                break

        return records[:max_results]


# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------


def write_csv(records: list[CaseRecord], output_path: str) -> None:
    """Write case records to a CSV file."""
    path = Path(output_path)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for rec in records:
            writer.writerow(asdict(rec))
    log.info(f"Wrote {len(records)} records to {path.resolve()}")


def deduplicate(records: list[CaseRecord]) -> list[CaseRecord]:
    """Remove duplicate records by (case_number, plaintiff_attorney)."""
    seen = set()
    deduped = []
    for rec in records:
        key = (rec.case_number, rec.plaintiff_attorney)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(rec)
    return deduped


# ---------------------------------------------------------------------------
# Main agent
# ---------------------------------------------------------------------------


def run_agent(
    api_token: Optional[str] = None,
    days_back: int = 90,
    max_results: int = 100,
    output: str = "medical_malpractice_cases.csv",
    include_pacer_rss: bool = True,
) -> None:
    """Main agent entry point: search, extract, deduplicate, write CSV."""

    log.info("=" * 60)
    log.info("Medical Malpractice Case Scraper Agent")
    log.info(f"Searching cases filed in the last {days_back} days")
    log.info(f"Max results: {max_results}")
    log.info("=" * 60)

    all_records: list[CaseRecord] = []

    # Source 1: CourtListener
    log.info("\n--- Source: CourtListener API ---")
    cl = CourtListenerSource(api_token=api_token, days_back=days_back)
    cl_records = cl.extract_records(max_results=max_results)
    log.info(f"CourtListener returned {len(cl_records)} records")
    all_records.extend(cl_records)

    # Source 2: PACER RSS (supplementary)
    if include_pacer_rss:
        log.info("\n--- Source: PACER RSS Feeds ---")
        pacer = PacerRSSSource()
        pacer_records = pacer.extract_records(max_results=max_results)
        log.info(f"PACER RSS returned {len(pacer_records)} records")
        all_records.extend(pacer_records)

    # Deduplicate
    all_records = deduplicate(all_records)
    log.info(f"\nTotal unique records: {len(all_records)}")

    # Write output
    write_csv(all_records, output)

    # Summary
    with_attorney = sum(1 for r in all_records if r.plaintiff_attorney)
    with_email = sum(1 for r in all_records if r.email)
    with_phone = sum(1 for r in all_records if r.phone)

    log.info("\n--- Summary ---")
    log.info(f"Total cases found:        {len(all_records)}")
    log.info(f"With attorney name:       {with_attorney}")
    log.info(f"With email:               {with_email}")
    log.info(f"With phone:               {with_phone}")
    log.info(f"Output file:              {output}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Search public court records for recently filed medical malpractice cases "
        "and extract attorney contact information.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic run — searches last 90 days, outputs to medical_malpractice_cases.csv
  python medical_malpractice_scraper.py

  # Search last 30 days with a CourtListener API token
  python medical_malpractice_scraper.py --days 30 --token YOUR_TOKEN

  # Output to a specific file, limit to 50 results
  python medical_malpractice_scraper.py --output results.csv --max 50

  # Skip PACER RSS feeds (CourtListener only)
  python medical_malpractice_scraper.py --no-pacer-rss
        """,
    )
    parser.add_argument(
        "--token",
        help="CourtListener API token (optional, increases rate limits). "
        "Get one free at https://www.courtlistener.com/sign-in/",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="Number of days back to search (default: 90)",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=100,
        help="Maximum number of records to return (default: 100)",
    )
    parser.add_argument(
        "--output",
        default="medical_malpractice_cases.csv",
        help="Output CSV file path (default: medical_malpractice_cases.csv)",
    )
    parser.add_argument(
        "--no-pacer-rss",
        action="store_true",
        help="Disable PACER RSS feed source",
    )

    args = parser.parse_args()

    run_agent(
        api_token=args.token,
        days_back=args.days,
        max_results=args.max,
        output=args.output,
        include_pacer_rss=not args.no_pacer_rss,
    )


if __name__ == "__main__":
    main()
