# Medical Malpractice Case Scraper

A Python agent that searches publicly accessible court record databases for recently filed medical malpractice cases and extracts attorney contact information.

## Data Sources

| Source | Type | Attorney Info | Cost |
|---|---|---|---|
| **CourtListener API** (primary) | REST API | Yes — name, firm, phone, email | Free |
| **PACER RSS Feeds** (supplementary) | RSS | No — case metadata only | Free |

### CourtListener (Free Law Project)

[CourtListener](https://www.courtlistener.com/) is a free, open platform run by the Free Law Project that aggregates millions of court records from PACER and state courts. Its REST API supports keyword search with date filtering and returns structured docket and party/attorney data.

- No API key required for basic usage (rate-limited to ~5 req/min)
- Free API tokens available at https://www.courtlistener.com/sign-in/ (higher limits)

### PACER RSS Feeds

Federal courts publish free RSS feeds of recently filed cases. These are parsed as a supplementary source to catch cases that may not yet be indexed by CourtListener. RSS entries provide case names and numbers but typically lack attorney contact details.

## Setup

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Basic run — searches last 90 days, outputs to medical_malpractice_cases.csv
python medical_malpractice_scraper.py

# Search last 30 days with a CourtListener API token (recommended)
python medical_malpractice_scraper.py --days 30 --token YOUR_TOKEN

# Output to a specific file, limit to 50 results
python medical_malpractice_scraper.py --output results.csv --max 50

# Skip PACER RSS feeds (CourtListener only)
python medical_malpractice_scraper.py --no-pacer-rss
```

## Output Format

The CSV file contains these columns:

| Column | Description |
|---|---|
| `case_number` | Docket/case number (e.g., `1:24-cv-01234`) |
| `case_name` | Full case name (e.g., `Smith v. General Hospital`) |
| `filing_date` | Date the case was filed |
| `court` | Court name or citation string |
| `plaintiff_attorney` | Name of the attorney representing the plaintiff |
| `law_firm` | Law firm name |
| `phone` | Attorney phone number (if available in court records) |
| `email` | Attorney email address (if available in court records) |
| `source_url` | Link to the case on CourtListener or PACER |

## Search Strategy

The agent searches across multiple query terms to maximize coverage:

- "medical malpractice"
- "medical negligence"
- "surgical error"
- "misdiagnosis"
- "failure to diagnose"

Results are deduplicated by (case_number, attorney_name) pairs.

## Rate Limiting

The scraper respects rate limits:
- 1-second delay between CourtListener API requests
- Automatic 30-second backoff on HTTP 429 responses
- Using a CourtListener API token increases your allowed rate

## Extending

To add additional court record sources, create a new class with an `extract_records(max_results) -> list[CaseRecord]` method and integrate it in the `run_agent()` function.
