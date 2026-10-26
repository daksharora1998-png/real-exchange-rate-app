# Real Exchange Rate Explorer

A Streamlit app that builds a bilateral real exchange rate (RER) between any two IMF economies (or the Euro area) from monthly IMF CPI and exchange-rate data, starting January 1976 where the IMF has coverage.

## Quick start (Python 3.12)

```bash
python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

The app opens at http://localhost:8501.

### Deploy on Streamlit Community Cloud

Push `app.py`, `requirements.txt` and `README.md` to a GitHub repo, create a new app pointing at `app.py`, and choose Python 3.12 under **Advanced settings**.

## Economics

| Symbol | Definition |
|---|---|
| S(t) | Units of Country 2 currency per 1 unit of Country 1 currency |
| P1(t), P2(t) | Monthly CPI of Country 1 and Country 2 |
| RER(t) | S(t) × P1(t) / P2(t) |
| RER* | Mean of RER(t) over **valid** months in the selected start–end window |
| Real Index(t) | 100 × RER(t) / RER* |

IMF exchange rates are national currency per USD (`XDC_USD`), so

    S(t) = [Country 2 currency per USD] / [Country 1 currency per USD]

The US rate is fixed at 1 (USD is the numeraire), so it is not downloaded.

**Reading the chart:** a rising Real Index means Country 1's consumption basket is getting more expensive relative to Country 2's — a real appreciation of Country 1's currency. 100 is the window average, so the index is a deviation-from-mean measure, not a PPP level (CPI base years differ across countries and cancel out in the normalisation).

A month is **valid** only when all four inputs (two CPIs, two USD rates) exist and are positive. Invalid months are excluded from RER*, shown as gaps in the charts, and kept in the CSV with `valid_month = False`. Changing the date window recomputes RER* and the index immediately.

## Features

- Country 1 / Country 2 from IMF member economies plus the Euro area (`G163`); a sidebar field accepts any other IMF code.
- Date window slider over the months where the pair has valid data.
- Chart view: **Real**, **Nominal**, or **Both** (Real Index on the left axis, nominal S(t) on the right axis). Nominal S(t) is always in actual currency units, never normalised; an optional log scale helps for high-inflation currencies.
- Summary metrics: RER*, latest Real Index, latest S(t), valid-month count.
- Coverage table showing first/last observation and missing months for each of the four input series, plus the exact API request URLs.
- CSV download of every month in the selected window.

### CSV columns

`month, country1, country2, country1_currency_per_USD, country2_currency_per_USD, S_country2_per_country1, P1_cpi_country1, P2_cpi_country2, valid_month, RER = S*P1/P2, RER_star (window mean), real_index = 100*RER/RER_star`

## Data source

IMF SDMX 3.0 API, base `https://api.imf.org/external/sdmx/3.0`, agency `IMF.STA`:

| Series | Dataflow | Key |
|---|---|---|
| CPI, all items, index, monthly | `CPI` | `{ISO3}.CPI._T.IX.M` |
| Exchange rate, national currency per USD, period average, monthly | `ER` | `{ISO3}.XDC_USD.PA_RT.M` |

Example request:

    https://api.imf.org/external/sdmx/3.0/data/dataflow/IMF.STA/CPI/+/USA.CPI._T.IX.M?dimensionAtObservation=TIME_PERIOD

The fetcher asks for SDMX-JSON first and falls back to SDMX-ML (XML); the parser handles both, plus `YYYY-MM` and `YYYY-Mmm` period formats. Transient errors (429/5xx) are retried with backoff. A 404 from the IMF means "no data for this key" and is reported as an empty series rather than an error.

Responses are cached for 12 hours per economy; the sidebar button **Clear cached IMF data** forces a refresh.

### Optional API key

The public endpoint does not normally need a key. If your network or the IMF requires one, set it as an environment variable or Streamlit secret and it will be sent as `Ocp-Apim-Subscription-Key`:

```bash
export IMF_API_KEY="your-key"
```

or in `.streamlit/secrets.toml`:

```toml
IMF_API_KEY = "your-key"
```

## Data caveats

- **Gaps and short coverage are common.** Many economies start well after 1976, have interior gaps, or publish CPI only quarterly. The app uses whatever monthly data exists and reports what is missing.
- **Euro area (`G163`)** euro exchange-rate data begins in 1999, so pairs involving `G163` start no earlier than that. For longer euro-member histories, pick the individual country (e.g. `DEU`, `FRA`) and check the coverage table.
- **Currency units are as the IMF reports them** ("domestic currency"). Redenominations (e.g. Türkiye 2005, Venezuela, Zimbabwe) or a switch to the euro can create level breaks in nominal S(t). The currency codes shown in labels are current codes, used for readability only.
- **Dollarised or pegged economies** may show a flat nominal S(t) against the USD; the real index then moves only with relative inflation.
- If the IMF changes dataflow versions or codes, the request URLs in the coverage panel are the first thing to check.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| "No month has all four inputs" | One of the four series is unavailable monthly for that economy; see the coverage table. |
| HTTP 403/401 errors | Corporate proxy or IMF access policy; try `IMF_API_KEY` or another network. |
| Timeouts | IMF API under load; wait and click **Clear cached IMF data**. |
| Odd jumps in nominal S(t) | Currency redenomination in IMF source data. |
