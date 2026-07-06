# Synthetic FSI Multi-Agent Demo Dataset

**All files in this directory contain SYNTHETIC / FICTIONAL DATA for demonstration only.** Company names, tickers, financial results, valuation metrics, transactions, and market datapoints are invented and should not be used for investment decisions.

## Files

### `companies.json`
Fictional company universe for the demo: NovaGrid Technologies (`NGTX`) plus six peers. Fields include fake ticker, company name, sector, sub-industry, business description, HQ country, employee count, and founded year.

### `novagrid_financials.csv`
Five-year historical financial statements for NovaGrid, FY2021A-FY2025A, in USD millions. Columns include income statement line items (`revenue`, `cogs`, `gross_profit`, `sga`, `rd`, `ebitda`, `da`, `ebit`, `interest_expense`, `pretax_income`, `taxes`, `net_income`), balance sheet line items (`total_assets`, `cash`, `ar`, `inventory`, `ppe`, `goodwill`, `total_debt`, `total_equity`), and cash flow line items (`cfo`, `capex`, `cfi`, `cff`). Negative cash flow numbers represent outflows.

### `novagrid_assumptions.json`
DCF assumptions for NovaGrid. Includes FY2026E-FY2030E revenue growth, EBITDA margin, tax rate, capex as a percent of revenue, net working capital as a percent of revenue, WACC inputs, terminal growth, current share price, shares outstanding, cash, debt, net debt, market capitalization, and enterprise value.

### `peer_comps.csv`
Comparable-company trading metrics for NovaGrid and six fictional peers, in USD millions except percentages and multiples. Columns include market capitalization, enterprise value, LTM revenue, LTM EBITDA, EV/Revenue, EV/EBITDA, P/E, revenue growth, EBITDA margin, and net debt/EBITDA. NovaGrid LTM EBITDA matches FY2025A EBITDA in `novagrid_financials.csv`.

### `lbo_assumptions.json`
Private equity LBO screening assumptions for NovaGrid. Includes entry EV/EBITDA multiple, entry EBITDA, purchase price, transaction fees, debt tranches with rates and amounts, sponsor equity contribution, five-year operating assumptions, cash sweep, minimum cash balance, exit multiple, and exit year. Entry EBITDA matches FY2025A EBITDA in `novagrid_financials.csv`.

### `market_context.json`
Fictional macro, sector, and M&A context for pitch and competitive-analysis narratives. Includes synthetic interest-rate and spread assumptions, sector growth datapoints, recent fictional smart-energy / industrial IoT transactions, and pitch narrative themes.
