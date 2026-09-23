"""SEC selection and bounded official-provider behavior, with mocked HTTP only."""
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import importlib.util
from urllib.error import HTTPError

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "financial_sec_provider_test", SERVER_DIR / "financial_fact_pack.py")
sec = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sec)
ACC = "0001193125-26-323660"


@pytest.fixture
def fixture():
    return json.loads((Path(__file__).parent / "fixtures" / "financial_msft_sec_20260916.json").read_text())


def normalize(f):
    return sec.normalize_fact_pack(f["submissions"], f["companyfacts"], "MSFT", "2026-09-16", "0000789019")


def revenue_rows(f):
    return f["companyfacts"]["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"]["USD"]


@pytest.mark.parametrize("mutate", [
    {"start": "2026-04-01"}, {"frame": "CY2026Q4"}, {"segment": {"member": "Cloud"}},
    {"dimensions": {"SegmentAxis": "Cloud"}}, {"context": "segment"},
    {"accn": "0001193125-25-000001"}, {"filed": "2026-09-17"}, {"form": "10-Q"},
])
def test_revenue_rejects_quarter_segment_other_accession_and_after_cutoff(fixture, mutate):
    rows = revenue_rows(fixture)
    rows[:] = [{**row, **mutate} for row in rows]
    result = normalize(fixture)
    assert result["facts"]["revenue"]["value"] is None
    assert result["facts"]["revenue"]["status"] == "missing"


def test_wrong_unit_missing_not_zero(fixture):
    source = fixture["companyfacts"]["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]
    source["units"]["EUR"] = source["units"].pop("USD")
    assert normalize(fixture)["facts"]["revenue"]["value"] is None


def test_conflicting_annual_revenue_aliases_are_not_preferred_silently(fixture):
    row = next(x for x in revenue_rows(fixture) if x["end"] == "2026-06-30")
    fixture["companyfacts"]["facts"]["us-gaap"]["Revenues"] = {"units": {"USD": [{**row, "val": 123}]}}
    fact = normalize(fixture)["facts"]["revenue"]
    assert fact["status"] == "conflict" and fact["value"] is None


def test_same_value_different_annual_start_is_conflict(fixture):
    row = next(x for x in revenue_rows(fixture) if x["end"] == "2026-06-30")
    revenue_rows(fixture).append({**row, "start": "2025-06-30"})
    assert normalize(fixture)["facts"]["revenue"]["status"] == "conflict"


def test_quarter_never_wins_when_annual_exists(fixture):
    row = next(x for x in revenue_rows(fixture) if x["end"] == "2026-06-30")
    revenue_rows(fixture).append({**row, "val": 123, "start": "2026-04-01", "frame": "CY2026Q4"})
    assert normalize(fixture)["facts"]["revenue"]["value"] == 331839000000


def test_instant_rejects_duration(fixture):
    items = fixture["companyfacts"]["facts"]["us-gaap"]["CashAndCashEquivalentsAtCarryingValue"]["units"]["USD"]
    for row in items:
        row["start"] = "2025-07-01"
    assert normalize(fixture)["facts"]["cash"]["status"] == "missing"


@pytest.mark.parametrize("source", ["submissions", "companyfacts"])
def test_wrong_entity_is_provider_error(fixture, source):
    fixture[source]["cik"] = 1
    with pytest.raises(sec.FactPackError, match="CIK differs"):
        normalize(fixture)


def test_wrong_ticker_is_provider_error(fixture):
    fixture["submissions"]["tickers"] = ["NOTMSFT"]
    with pytest.raises(sec.FactPackError, match="Ticker does not match"):
        normalize(fixture)


def test_amendment_does_not_backfill_original(fixture):
    rows = fixture["submissions"]["filings"]["recent"]
    for key, values in rows.items():
        values.append(values[0])
    rows["form"][-1] = "10-K/A"
    rows["filingDate"][-1] = "2026-08-20"
    rows["accessionNumber"][-1] = "0001193125-26-999999"
    p = normalize(fixture)
    assert p["filings"][0]["form"] == "10-K/A"
    assert p["facts"]["revenue"]["value"] is None
    assert any("not backfilled" in x for x in p["warnings"])


def test_unsupported_foreign_form_and_cutoff(fixture):
    fixture["submissions"]["filings"]["recent"]["form"][0] = "20-F"
    with pytest.raises(sec.FactPackError, match="unsupported"):
        normalize(fixture)


def test_debt_and_cash_components_never_double_count(fixture):
    f = fixture["companyfacts"]["facts"]["us-gaap"]
    f.pop("LongTermDebt")
    f.pop("CashCashEquivalentsAndShortTermInvestments")
    p = normalize(fixture)
    assert p["facts"]["long_term_debt"]["value"] == 40294000000
    assert p["facts"]["cash_and_investments"]["value"] == 76843000000
    assert p["facts"]["long_term_debt"]["components"] == ["debt_current", "debt_noncurrent"]


def test_conflicting_debt_subtotal_null(fixture):
    items = fixture["companyfacts"]["facts"]["us-gaap"]["LongTermDebt"]["units"]["USD"]
    for row in items:
        if row["end"] == "2026-06-30":
            row["val"] = 10
    assert normalize(fixture)["facts"]["long_term_debt"]["status"] == "conflict"


def test_provider_required_contact_and_explicit_failure(monkeypatch):
    monkeypatch.delenv("SEC_EDGAR_USER_AGENT", raising=False)
    with pytest.raises(sec.FactPackError, match="contact"):
        sec.get_financial_fact_pack("MSFT", "2026-09-16")
    monkeypatch.setattr(sec, "urlopen", lambda *a, **k: (_ for _ in ()).throw(
        HTTPError(a[0].full_url, 429, "rate limit", {}, None)))
    with pytest.raises(sec.FactPackError) as error:
        sec._fetch("https://data.sec.gov/submissions/CIK0000789019.json", "Test test@example.test")
    assert error.value.code == "sec_rate_limited"


def test_provider_official_resolution_cutoff_cache_and_bounded_history(fixture, monkeypatch):
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "Tests tests@example.test")
    sec._packs.clear()
    sec._tickers = None
    calls = []
    def fetch(url, agent):
        calls.append(url)
        if url.endswith("company_tickers.json"):
            return {"0": {"ticker": "MSFT", "cik_str": 789019}}
        if "companyfacts" in url:
            return deepcopy(fixture["companyfacts"])
        return deepcopy(fixture["submissions"])
    monkeypatch.setattr(sec, "_fetch", fetch)
    one = sec.get_financial_fact_pack("MSFT", "2026-09-16")
    assert len(calls) == 3
    one["facts"]["revenue"]["value"] = 0
    two = sec.get_financial_fact_pack("msft", "2026-09-16")
    assert two["facts"]["revenue"]["value"] == 331839000000 and len(calls) == 3
    sec.get_financial_fact_pack("MSFT", "2026-09-15")
    assert len(calls) == 5


def test_history_fetch_bound_and_unavailable_failure(fixture, monkeypatch):
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "Tests tests@example.test")
    sec._packs.clear()
    submissions = deepcopy(fixture["submissions"])
    recent = submissions["filings"]["recent"]
    empty = {key: [] for key in recent}
    submissions["filings"] = {"recent": empty, "files": [
        {"name": f"CIK0000789019-submissions-{i:03}.json", "filingFrom": "2000-01-01", "filingTo": "2020-01-01"}
        for i in range(12)]}
    calls = []
    def fetch(url, agent):
        calls.append(url)
        return submissions if url.endswith("CIK0000789019.json") else empty
    monkeypatch.setattr(sec, "_fetch", fetch)
    with pytest.raises(sec.FactPackError, match="bounded"):
        sec.get_financial_fact_pack("MSFT", "2026-09-16", "789019")
    assert len(calls) == 6


@pytest.mark.parametrize("ticker,asof,cik", [
    ("../SEC", "2026-09-16", None), ("MSFT", "not-date", None),
    ("MSFT", "2026-09-16", "abc"), ("MSFT", "2999-01-01", None),
])
def test_provider_rejects_invalid_inputs(ticker, asof, cik):
    with pytest.raises(sec.FactPackError):
        sec.get_financial_fact_pack(ticker, asof, cik)


def test_api_only_fixture_matches_raw_official_normalization(fixture):
    normalized = json.loads((Path(__file__).parent / "fixtures" / "financial_msft_fact_pack_20260916.json").read_text())
    result = normalize(fixture)
    result["retrieved_at"] = normalized["retrieved_at"]
    assert result == normalized


def test_official_fetch_is_rate_limited_bounded_and_rejects_non_json(monkeypatch):
    clock = [100.0]
    sleeps = []
    class Response:
        data = b'{"ok":true}'
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def geturl(self):
            return "https://data.sec.gov/submissions/CIK0000789019.json"
        def read(self, limit):
            assert limit == sec.MAX_RESPONSE + 1
            return self.data[:limit]
    def sleep(duration):
        sleeps.append(duration)
        clock[0] += duration
    monkeypatch.setattr(sec.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(sec.time, "sleep", sleep)
    monkeypatch.setattr(sec, "_next_request", 100.0)
    monkeypatch.setattr(sec, "urlopen", lambda *a, **kw: Response())
    url = "https://data.sec.gov/submissions/CIK0000789019.json"
    assert sec._fetch(url, "Tests tests@example.test") == {"ok": True}
    assert sec._fetch(url, "Tests tests@example.test") == {"ok": True}
    assert sleeps == [0, 0.5]
    monkeypatch.setattr(sec, "MAX_RESPONSE", 2)
    with pytest.raises(sec.FactPackError, match="limit"):
        sec._fetch(url, "Tests tests@example.test")
    monkeypatch.setattr(sec, "MAX_RESPONSE", 100)
    monkeypatch.setattr(Response, "data", b"not JSON")
    with pytest.raises(sec.FactPackError, match="malformed JSON"):
        sec._fetch(url, "Tests tests@example.test")
    with pytest.raises(sec.FactPackError, match="nonofficial"):
        sec._fetch("https://example.com/data", "Tests tests@example.test")
