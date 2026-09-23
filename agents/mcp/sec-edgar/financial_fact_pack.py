"""Bounded SEC entity-wide annual normalization; never an audit of complete filings.

Interfaces follow SEC submissions/companyfacts documentation:
https://www.sec.gov/search-filings/edgar-application-programming-interfaces
https://www.sec.gov/about/developer-resources
Only official hosts are fetched. Contact identification is mandatory; this tool
serializes requests at two per second, below SEC's ten-request/second ceiling.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from datetime import date, datetime, timezone
import json
import math
import os
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

MAX_RESPONSE = 32 * 1024 * 1024
MAX_HISTORY = 5
_lock = threading.Lock()
_next_request = 0.0
_packs: OrderedDict = OrderedDict()
_tickers: tuple[float, dict] | None = None
_cache_lock = threading.Lock()

METRICS = {
    "revenue": ("Revenue", "annual", ["RevenueFromContractWithCustomerExcludingAssessedTax",
                                      "Revenues", "SalesRevenueNet"]),
    "operating_income": ("Operating income", "annual", ["OperatingIncomeLoss"]),
    "net_income": ("Net income attributable to parent", "annual", ["NetIncomeLoss"]),
    "diluted_shares": ("Weighted-average diluted shares", "annual",
                       ["WeightedAverageNumberOfDilutedSharesOutstanding"]),
    "operating_cash_flow": ("Operating cash flow", "annual",
                            ["NetCashProvidedByUsedInOperatingActivities"]),
    "cash_capex": ("Cash purchases of PP&E", "annual", ["PaymentsToAcquirePropertyPlantAndEquipment"]),
    "depreciation_amortization": ("Pure depreciation and amortization", "annual",
                                ["DepreciationAndAmortization"]),
    "cash": ("Cash and equivalents", "instant", ["CashAndCashEquivalentsAtCarryingValue"]),
    "short_term_investments": ("Short-term investments", "instant", ["ShortTermInvestments"]),
    "cash_and_investments": ("Cash plus short-term investments", "instant",
                             ["CashCashEquivalentsAndShortTermInvestments"]),
    "long_term_debt": ("Long-term debt including current maturities", "instant", ["LongTermDebt"]),
    "debt_current": ("Current maturities of long-term debt", "instant", ["LongTermDebtCurrent"]),
    "debt_noncurrent": ("Noncurrent long-term debt", "instant", ["LongTermDebtNoncurrent"]),
    "assets": ("Total assets", "instant", ["Assets"]),
    "liabilities": ("Total liabilities", "instant", ["Liabilities"]),
    "equity": ("Stockholders' equity (parent)", "instant", ["StockholdersEquity"]),
}


class FactPackError(ValueError):
    def __init__(self, message, code="sec_provider_error"):
        super().__init__(message)
        self.code = code


def _date(value, field):
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError()
        return date.fromisoformat(value)
    except ValueError as exc:
        raise FactPackError(f"Invalid {field}; expected YYYY-MM-DD", "invalid_input") from exc


def _cik(value):
    text = str(value)
    if not re.fullmatch(r"[0-9]{1,10}", text) or int(text) == 0:
        raise FactPackError("CIK must be 1-10 ASCII digits and nonzero", "invalid_input")
    return text.zfill(10)


def _fetch(url, user_agent):
    global _next_request
    if not re.fullmatch(r"https://(?:data\.sec\.gov|www\.sec\.gov)/[A-Za-z0-9/_.-]+", url):
        raise FactPackError("Rejected nonofficial SEC URL", "invalid_input")
    try:
        with _lock:
            time.sleep(max(0, _next_request - time.monotonic()))
            _next_request = time.monotonic() + 0.5
            request = Request(url, headers={"User-Agent": user_agent, "Accept": "application/json"})
            with urlopen(request, timeout=30) as response:
                if response.geturl() != url:
                    raise FactPackError("SEC redirected the data request; refusing unverified response")
                raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise FactPackError("SEC response exceeds 32 MiB limit")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise FactPackError("SEC returned a non-object JSON payload")
        return result
    except HTTPError as exc:
        raise FactPackError(f"Official SEC request failed with HTTP {exc.code}; no fallback data",
                            "sec_rate_limited" if exc.code == 429 else "sec_http_error") from exc
    except (URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FactPackError("Official SEC request failed or returned malformed JSON; no fallback data") from exc


def _filing_rows(block, cutoff):
    required = ("form", "filingDate", "reportDate", "accessionNumber", "primaryDocument")
    if any(not isinstance(block.get(key), list) for key in required):
        raise FactPackError("SEC submissions filing arrays are malformed")
    lengths = {len(block[key]) for key in required}
    if len(lengths) != 1:
        raise FactPackError("SEC submissions filing arrays have inconsistent lengths")
    result = []
    for form, filed, end, accn, document in zip(*(block[k] for k in required)):
        if form not in {"10-K", "10-K/A"}:
            continue
        if _date(filed, "filing date") > cutoff or not end or _date(end, "period end") > cutoff:
            continue
        if not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", accn):
            raise FactPackError("SEC filing has malformed accession")
        result.append({"form": form, "filed": filed, "period_end": end,
                       "accession": accn, "document": document})
    return result


def select_filing(submissions, cutoff, history=()):
    rows = _filing_rows(submissions.get("filings", {}).get("recent", {}), cutoff)
    for block in history:
        rows.extend(_filing_rows(block, cutoff))
    if not rows:
        raise FactPackError("No eligible 10-K/10-K/A within bounded submissions history. "
                            "20-F, 40-F, 10-Q and custom-taxonomy-only issuers are unsupported.",
                            "unsupported_filing")
    return max(rows, key=lambda x: (x["period_end"], x["filed"], x["accession"]))


def normalize_fact_pack(submissions: dict, companyfacts: dict, ticker: str, as_of: str,
                        cik: str, history=()) -> dict:
    """Pure normalizer, also used with captured official responses in regression tests."""
    cutoff = _date(as_of, "as_of")
    cik = _cik(cik)
    if _cik(submissions.get("cik", "")) != cik or _cik(companyfacts.get("cik", "")) != cik:
        raise FactPackError("SEC entity CIK differs from requested entity", "entity_mismatch")
    if ticker.upper() not in [str(t).upper() for t in submissions.get("tickers", [])]:
        raise FactPackError("Ticker does not match SEC submissions entity", "entity_mismatch")
    filing = select_filing(submissions, cutoff, history)
    url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
           f"{filing['accession'].replace('-', '')}/{quote(filing.pop('document'), safe='')}")
    filing["url"] = url
    taxonomy = companyfacts.get("facts", {}).get("us-gaap", {})
    if not isinstance(taxonomy, dict):
        raise FactPackError("SEC us-gaap facts are malformed")
    facts = {}
    for metric, (label, period, concepts) in METRICS.items():
        unit = "shares" if metric == "diluted_shares" else "USD"
        matches = []
        rejected = 0
        for concept in concepts:
            for item in taxonomy.get(concept, {}).get("units", {}).get(unit, []):
                if (item.get("accn") != filing["accession"] or item.get("end") != filing["period_end"]
                        or item.get("form") != filing["form"] or not item.get("filed")
                        or _date(item["filed"], "fact filed") != _date(filing["filed"], "filing date")):
                    continue
                if item.get("segment") or item.get("dimensions") or item.get("context"):
                    rejected += 1
                    continue
                if period == "annual":
                    if not item.get("start"):
                        rejected += 1
                        continue
                    days = (_date(item["end"], "fact end") - _date(item["start"], "fact start")).days
                    if not 350 <= days <= 380 or "Q" in item.get("frame", ""):
                        rejected += 1
                        continue
                elif item.get("start"):
                    rejected += 1
                    continue
                value = item.get("val")
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise FactPackError(f"SEC fact {concept} is not a finite number")
                matches.append((concept, item))
        signatures = {(x["val"], x.get("start")) for _, x in matches}
        status = "source_backed" if len(signatures) == 1 else "conflict" if signatures else "missing"
        selected = matches[0][1] if status == "source_backed" else {}
        reason = ("Exact accession/entity, annual duration or matching instant, and unit."
                  if status == "source_backed" else
                  "Conflicting values or annual contexts across eligible concepts; no alias chosen."
                  if status == "conflict" else
                  "No eligible standardized entity-wide fact in selected filing; not assumed zero.")
        facts[metric] = {
            "id": metric, "label": label, "value": selected.get("val"), "unit": unit,
            "status": status, "concept": "|".join(sorted({c for c, _ in matches})) or None,
            "start": selected.get("start"), "end": filing["period_end"],
            "filing_accession": filing["accession"], "source_url": url, "reason": reason,
            "rejected_contexts": rejected,
        }
    _subtotal(facts, "cash_and_investments", "cash", "short_term_investments")
    _subtotal(facts, "long_term_debt", "debt_current", "debt_noncurrent")
    warnings = [f"{key}: {fact['reason']}" for key, fact in facts.items()
                if fact["status"] != "source_backed"]
    if filing["form"] == "10-K/A":
        warnings.append("Selected amendment only; missing facts are not backfilled from the original 10-K.")
    return {
        "schema_version": "1.0", "entity": {"cik": cik, "ticker": ticker.upper(),
                                           "name": submissions.get("name", "")},
        "as_of": as_of, "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "filings": [filing], "facts": facts, "warnings": warnings,
        "limitations": [
            "Selected standardized entity-wide US-GAAP 10-K/10-K/A facts, not a full-source audit.",
            "Latest eligible annual filing only; historical lookup is bounded to five SEC files.",
            "20-F, 40-F, quarterly, segment and custom-taxonomy-only financials are unsupported.",
            "Pure D&A excludes combined depletion, accretion and other noncash-adjustment concepts.",
            "Long-term debt includes current maturities; total transaction debt is NOT certified. "
            "Commercial paper, leases, pensions and other debt-like claims may require adjustments.",
            "Cash plus short-term investments is not all investments or necessarily distributable cash.",
            "Current SEC ticker mapping is not a historical ticker-ownership registry.",
        ],
    }


def _subtotal(facts, total, left, right):
    t, a, b = (facts[k] for k in (total, left, right))
    if a["status"] != "source_backed" or b["status"] != "source_backed":
        return
    value = a["value"] + b["value"]
    if t["status"] == "source_backed" and t["value"] != value:
        t.update(value=None, status="conflict", reason="Reported subtotal disagrees with components.")
    elif t["status"] == "missing":
        t.update(value=value, status="source_backed", concept=f"{a['concept']} + {b['concept']}",
                 reason="Derived exactly from separately sourced components, not an additional balance.",
                 components=[left, right])


def get_financial_fact_pack(ticker: str, as_of: str, cik: str | None = None) -> dict:
    """Get normalized financial fact pack: annual consolidated historical revenue,
    cash flow, debt, balance sheet, diluted shares, reporting period and units
    from official SEC evidence as known at YYYY-MM-DD.

    Missing/conflicting facts are null. No quotes, estimates, quarterly substitutions
    or synthetic fallbacks. US-GAAP 10-K/10-K/A only; exact filing provenance included.
    """
    global _tickers
    if not isinstance(ticker, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,14}", ticker):
        raise FactPackError("Ticker must be 1-15 ASCII letters/digits/dots/dashes", "invalid_input")
    ticker = ticker.upper()
    cutoff = _date(as_of, "as_of")
    if cutoff > datetime.now(timezone.utc).date() or cutoff.year < 1994:
        raise FactPackError("as_of must be between 1994 and today", "invalid_input")
    user_agent = os.environ.get("SEC_EDGAR_USER_AGENT", "").strip()
    if not user_agent or not re.search(r"[^@\s]+@[^@\s]+\.[^@\s]+", user_agent):
        raise FactPackError("SEC_EDGAR_USER_AGENT with organization and contact email is required",
                            "provider_configuration")
    requested_cik = _cik(cik) if cik is not None else None
    key = (ticker, as_of, requested_cik)
    with _cache_lock:
        cached = _packs.get(key)
        if cached and time.monotonic() - cached[0] < 3600:
            _packs.move_to_end(key)
            return deepcopy(cached[1])
    if requested_cik is None:
        with _cache_lock:
            tickers = _tickers
        if not tickers or time.monotonic() - tickers[0] > 86400:
            data = _fetch("https://www.sec.gov/files/company_tickers.json", user_agent)
            with _cache_lock:
                _tickers = (time.monotonic(), data)
        else:
            data = tickers[1]
        matches = {_cik(x["cik_str"]) for x in data.values()
                   if isinstance(x, dict) and x.get("ticker", "").upper() == ticker}
        if len(matches) != 1:
            raise FactPackError("Ticker was not uniquely resolved in official SEC mapping",
                                "entity_not_found")
        requested_cik = matches.pop()
    submissions = _fetch(f"https://data.sec.gov/submissions/CIK{requested_cik}.json", user_agent)
    history = []
    recent = _filing_rows(submissions.get("filings", {}).get("recent", {}), cutoff)
    # Older files cannot supersede an eligible recent filing (SEC files are chronological).
    if not recent:
        files = submissions.get("filings", {}).get("files", [])
        eligible = [f for f in files if _date(f["filingFrom"], "history start") <= cutoff]
        for file in sorted(eligible, key=lambda f: f["filingTo"], reverse=True)[:MAX_HISTORY]:
            name = file.get("name", "")
            if not re.fullmatch(rf"CIK{requested_cik}-submissions-[0-9]+\.json", name):
                raise FactPackError("Unexpected SEC historical submissions filename")
            history.append(_fetch(f"https://data.sec.gov/submissions/{name}", user_agent))
        select_filing(submissions, cutoff, history)
    companyfacts = _fetch(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{requested_cik}.json", user_agent)
    result = normalize_fact_pack(submissions, companyfacts, ticker, as_of, requested_cik, history)
    with _cache_lock:
        _packs[key] = (time.monotonic(), deepcopy(result))
        _packs.move_to_end(key)
        while len(_packs) > 64:
            _packs.popitem(last=False)
    return result
