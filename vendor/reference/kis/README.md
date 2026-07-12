# KIS Reference Archive

Restored on 2026-05-16 after cleanup.

This directory is a protected vendor/reference area for Korea Investment Securities
Open API materials. Do not delete during cleanup unless the project owner explicitly
confirms removal.

## Sources

- Official KIS Developers portal: https://apiportal.koreainvestment.com/
- Official sample repository: https://github.com/koreainvestment/open-trading-api

## Contents

- `official_portal/html/`
  - Public portal page snapshots for API guide, category files, and error code pages.
- `official_portal/json/`
  - Public API detail JSON snapshots for endpoints used by the project.
  - Token response samples are excluded, even when expired, to keep public
    secret scanners quiet.
- `official_portal/excel/`
  - Downloaded official Excel guide files.
  - `domestic_stock_inquire_time_dailychartprice.xlsx`
  - `domestic_stock_inquire_investor.xlsx`
  - `domestic_stock_foreign_institution_total.xlsx`
  - `domestic_stock_basic_quotes_collection.xlsx`
  - `domestic_stock_market_analysis_collection.xlsx`
  - `domestic_stock_trading_account_collection.xlsx`
  - `domestic_stock_realtime_quotes_collection.xlsx`
  - `oauth_collection.xlsx`
- `github_sample/`
  - README and docs copied from the official KIS sample repository restored at
    `open-trading-api-main/`.

## Notes

- The previously deleted local files named `KIS api ...` and
  `...OpenAPI...20260514...xlsx` were not found in local search, Trash, or the
  restored zip source.
- The restored Excel files above are fresh downloads from the public KIS portal.
- No `.env`, access token, or credential-like files are stored here.
