"""
Real Exchange Rate (RER) Explorer
=================================

Streamlit app that pulls monthly CPI and exchange-rate data from the IMF
SDMX 3.0 API and computes a bilateral real exchange rate.

Definitions
-----------
S(t)            units of Country 2 currency per 1 unit of Country 1 currency
P1(t), P2(t)    monthly CPI of Country 1 and Country 2
RER(t)          S(t) * P1(t) / P2(t)
RER*            mean of RER(t) over valid months in the selected window
Real Index(t)   100 * RER(t) / RER*

IMF exchange rates are national-currency units per USD (XDC per USD), so
S(t) = [C2 per USD] / [C1 per USD].

Run:  streamlit run app.py
"""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from plotly.subplots import make_subplots
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
API_BASE = "https://api.imf.org/external/sdmx/3.0"
AGENCY = "IMF.STA"
CPI_FLOW = "CPI"
ER_FLOW = "ER"
CPI_KEY = "{iso}.CPI._T.IX.M"
ER_KEY = "{iso}.XDC_USD.PA_RT.M"
START_PERIOD = pd.Period("1976-01", freq="M")
EURO_AREA = "G163"
NUMERAIRE = "USA"  # IMF rates are per USD, so the US rate is identically 1
CACHE_TTL = 60 * 60 * 12  # 12 hours
REQUEST_TIMEOUT = (10, 90)  # (connect, read) seconds

# Accept headers tried in order. The parser sniffs the body, so either JSON or
# XML works; later entries are only used if the server rejects earlier ones.
ACCEPT_HEADERS = [
    "application/vnd.sdmx.data+json;version=2.0.0",
    "application/json",
    "application/vnd.sdmx.structurespecificdata+xml;version=3.0.0",
    "application/xml",
]

# ISO3 -> (display name, current currency code). Currency codes are labels
# only; IMF reports values in "domestic currency" (XDC) units, which can
# reflect past redenominations or currency changes.
COUNTRIES: dict[str, tuple[str, str]] = {
    "G163": ("Euro area", "EUR"),
    "AFG": ("Afghanistan", "AFN"), "ALB": ("Albania", "ALL"),
    "DZA": ("Algeria", "DZD"), "AND": ("Andorra", "EUR"),
    "AGO": ("Angola", "AOA"), "ATG": ("Antigua and Barbuda", "XCD"),
    "ARG": ("Argentina", "ARS"), "ARM": ("Armenia", "AMD"),
    "ABW": ("Aruba", "AWG"), "AUS": ("Australia", "AUD"),
    "AUT": ("Austria", "EUR"), "AZE": ("Azerbaijan", "AZN"),
    "BHS": ("Bahamas", "BSD"), "BHR": ("Bahrain", "BHD"),
    "BGD": ("Bangladesh", "BDT"), "BRB": ("Barbados", "BBD"),
    "BLR": ("Belarus", "BYN"), "BEL": ("Belgium", "EUR"),
    "BLZ": ("Belize", "BZD"), "BEN": ("Benin", "XOF"),
    "BTN": ("Bhutan", "BTN"), "BOL": ("Bolivia", "BOB"),
    "BIH": ("Bosnia and Herzegovina", "BAM"), "BWA": ("Botswana", "BWP"),
    "BRA": ("Brazil", "BRL"), "BRN": ("Brunei Darussalam", "BND"),
    "BGR": ("Bulgaria", "EUR"), "BFA": ("Burkina Faso", "XOF"),
    "BDI": ("Burundi", "BIF"), "CPV": ("Cabo Verde", "CVE"),
    "KHM": ("Cambodia", "KHR"), "CMR": ("Cameroon", "XAF"),
    "CAN": ("Canada", "CAD"), "CAF": ("Central African Republic", "XAF"),
    "TCD": ("Chad", "XAF"), "CHL": ("Chile", "CLP"),
    "CHN": ("China", "CNY"), "COL": ("Colombia", "COP"),
    "COM": ("Comoros", "KMF"), "COD": ("Congo, Dem. Rep.", "CDF"),
    "COG": ("Congo, Rep.", "XAF"), "CRI": ("Costa Rica", "CRC"),
    "CIV": ("Côte d'Ivoire", "XOF"), "HRV": ("Croatia", "EUR"),
    "CYP": ("Cyprus", "EUR"), "CZE": ("Czech Republic", "CZK"),
    "DNK": ("Denmark", "DKK"), "DJI": ("Djibouti", "DJF"),
    "DMA": ("Dominica", "XCD"), "DOM": ("Dominican Republic", "DOP"),
    "ECU": ("Ecuador", "USD"), "EGY": ("Egypt", "EGP"),
    "SLV": ("El Salvador", "USD"), "GNQ": ("Equatorial Guinea", "XAF"),
    "ERI": ("Eritrea", "ERN"), "EST": ("Estonia", "EUR"),
    "SWZ": ("Eswatini", "SZL"), "ETH": ("Ethiopia", "ETB"),
    "FJI": ("Fiji", "FJD"), "FIN": ("Finland", "EUR"),
    "FRA": ("France", "EUR"), "GAB": ("Gabon", "XAF"),
    "GMB": ("Gambia", "GMD"), "GEO": ("Georgia", "GEL"),
    "DEU": ("Germany", "EUR"), "GHA": ("Ghana", "GHS"),
    "GRC": ("Greece", "EUR"), "GRD": ("Grenada", "XCD"),
    "GTM": ("Guatemala", "GTQ"), "GIN": ("Guinea", "GNF"),
    "GNB": ("Guinea-Bissau", "XOF"), "GUY": ("Guyana", "GYD"),
    "HTI": ("Haiti", "HTG"), "HND": ("Honduras", "HNL"),
    "HKG": ("Hong Kong SAR", "HKD"), "HUN": ("Hungary", "HUF"),
    "ISL": ("Iceland", "ISK"), "IND": ("India", "INR"),
    "IDN": ("Indonesia", "IDR"), "IRN": ("Iran", "IRR"),
    "IRQ": ("Iraq", "IQD"), "IRL": ("Ireland", "EUR"),
    "ISR": ("Israel", "ILS"), "ITA": ("Italy", "EUR"),
    "JAM": ("Jamaica", "JMD"), "JPN": ("Japan", "JPY"),
    "JOR": ("Jordan", "JOD"), "KAZ": ("Kazakhstan", "KZT"),
    "KEN": ("Kenya", "KES"), "KIR": ("Kiribati", "AUD"),
    "KOR": ("Korea", "KRW"), "KWT": ("Kuwait", "KWD"),
    "KGZ": ("Kyrgyz Republic", "KGS"), "LAO": ("Lao P.D.R.", "LAK"),
    "LVA": ("Latvia", "EUR"), "LBN": ("Lebanon", "LBP"),
    "LSO": ("Lesotho", "LSL"), "LBR": ("Liberia", "LRD"),
    "LBY": ("Libya", "LYD"), "LIE": ("Liechtenstein", "CHF"),
    "LTU": ("Lithuania", "EUR"), "LUX": ("Luxembourg", "EUR"),
    "MAC": ("Macao SAR", "MOP"), "MDG": ("Madagascar", "MGA"),
    "MWI": ("Malawi", "MWK"), "MYS": ("Malaysia", "MYR"),
    "MDV": ("Maldives", "MVR"), "MLI": ("Mali", "XOF"),
    "MLT": ("Malta", "EUR"), "MHL": ("Marshall Islands", "USD"),
    "MRT": ("Mauritania", "MRU"), "MUS": ("Mauritius", "MUR"),
    "MEX": ("Mexico", "MXN"), "FSM": ("Micronesia", "USD"),
    "MDA": ("Moldova", "MDL"), "MNG": ("Mongolia", "MNT"),
    "MNE": ("Montenegro", "EUR"), "MAR": ("Morocco", "MAD"),
    "MOZ": ("Mozambique", "MZN"), "MMR": ("Myanmar", "MMK"),
    "NAM": ("Namibia", "NAD"), "NRU": ("Nauru", "AUD"),
    "NPL": ("Nepal", "NPR"), "NLD": ("Netherlands", "EUR"),
    "NZL": ("New Zealand", "NZD"), "NIC": ("Nicaragua", "NIO"),
    "NER": ("Niger", "XOF"), "NGA": ("Nigeria", "NGN"),
    "MKD": ("North Macedonia", "MKD"), "NOR": ("Norway", "NOK"),
    "OMN": ("Oman", "OMR"), "PAK": ("Pakistan", "PKR"),
    "PLW": ("Palau", "USD"), "PAN": ("Panama", "PAB"),
    "PNG": ("Papua New Guinea", "PGK"), "PRY": ("Paraguay", "PYG"),
    "PER": ("Peru", "PEN"), "PHL": ("Philippines", "PHP"),
    "POL": ("Poland", "PLN"), "PRT": ("Portugal", "EUR"),
    "QAT": ("Qatar", "QAR"), "ROU": ("Romania", "RON"),
    "RUS": ("Russia", "RUB"), "RWA": ("Rwanda", "RWF"),
    "WSM": ("Samoa", "WST"), "SMR": ("San Marino", "EUR"),
    "STP": ("São Tomé and Príncipe", "STN"), "SAU": ("Saudi Arabia", "SAR"),
    "SEN": ("Senegal", "XOF"), "SRB": ("Serbia", "RSD"),
    "SYC": ("Seychelles", "SCR"), "SLE": ("Sierra Leone", "SLE"),
    "SGP": ("Singapore", "SGD"), "SVK": ("Slovak Republic", "EUR"),
    "SVN": ("Slovenia", "EUR"), "SLB": ("Solomon Islands", "SBD"),
    "SOM": ("Somalia", "SOS"), "ZAF": ("South Africa", "ZAR"),
    "SSD": ("South Sudan", "SSP"), "ESP": ("Spain", "EUR"),
    "LKA": ("Sri Lanka", "LKR"), "KNA": ("St. Kitts and Nevis", "XCD"),
    "LCA": ("St. Lucia", "XCD"), "VCT": ("St. Vincent and the Grenadines", "XCD"),
    "SDN": ("Sudan", "SDG"), "SUR": ("Suriname", "SRD"),
    "SWE": ("Sweden", "SEK"), "CHE": ("Switzerland", "CHF"),
    "SYR": ("Syria", "SYP"), "TJK": ("Tajikistan", "TJS"),
    "TZA": ("Tanzania", "TZS"), "THA": ("Thailand", "THB"),
    "TLS": ("Timor-Leste", "USD"), "TGO": ("Togo", "XOF"),
    "TON": ("Tonga", "TOP"), "TTO": ("Trinidad and Tobago", "TTD"),
    "TUN": ("Tunisia", "TND"), "TUR": ("Türkiye", "TRY"),
    "TKM": ("Turkmenistan", "TMT"), "TUV": ("Tuvalu", "AUD"),
    "UGA": ("Uganda", "UGX"), "UKR": ("Ukraine", "UAH"),
    "ARE": ("United Arab Emirates", "AED"), "GBR": ("United Kingdom", "GBP"),
    "USA": ("United States", "USD"), "URY": ("Uruguay", "UYU"),
    "UZB": ("Uzbekistan", "UZS"), "VUT": ("Vanuatu", "VUV"),
    "VEN": ("Venezuela", "VES"), "VNM": ("Vietnam", "VND"),
    "YEM": ("Yemen", "YER"), "ZMB": ("Zambia", "ZMW"),
    "ZWE": ("Zimbabwe", "ZWG"),
}


# ---------------------------------------------------------------------------
# HTTP + SDMX parsing
# ---------------------------------------------------------------------------
@dataclass
class FetchResult:
    """Outcome of one IMF series request."""

    series: pd.Series = field(default_factory=lambda: pd.Series(dtype="float64"))
    status: str = "ok"  # ok | empty | error | synthetic
    url: str = ""
    message: str = ""


def _api_key() -> str | None:
    key = os.environ.get("IMF_API_KEY")
    if key:
        return key
    try:
        return st.secrets.get("IMF_API_KEY")  # type: ignore[no-any-return]
    except Exception:  # no secrets file configured
        return None


def _session() -> requests.Session:
    retry = Retry(
        total=4,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update(
        {
            "User-Agent": "rer-explorer/1.0 (Streamlit; python-requests)",
            "Accept-Encoding": "gzip, deflate",
        }
    )
    key = _api_key()
    if key:
        s.headers["Ocp-Apim-Subscription-Key"] = key
    return s


_MONTH_RE = re.compile(r"^\s*(\d{4})\s*-?\s*M?\s*(\d{1,2})\s*$", re.IGNORECASE)


def _to_period(label: str) -> pd.Period | None:
    """Parse IMF monthly labels such as '2020-M01', '2020M1' or '2020-01'."""
    m = _MONTH_RE.match(str(label))
    if not m:
        return None
    month = int(m.group(2))
    if not 1 <= month <= 12:
        return None
    return pd.Period(year=int(m.group(1)), month=month, freq="M")


def _finalise(records: dict[str, object]) -> pd.Series:
    """Turn {time_label: value} into a clean, sorted monthly float Series."""
    periods, values = [], []
    for label, value in records.items():
        p = _to_period(label)
        if p is None:
            continue
        try:
            v = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if np.isfinite(v):
            periods.append(p)
            values.append(v)
    if not periods:
        return pd.Series(dtype="float64")
    s = pd.Series(values, index=pd.PeriodIndex(periods, freq="M"), dtype="float64")
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s[s.index >= START_PERIOD]


def _time_label(value_obj: dict) -> str:
    for k in ("id", "value", "name"):
        v = value_obj.get(k)
        if isinstance(v, str):
            return v
        if isinstance(v, dict) and v:  # localised name, e.g. {"en": "..."}
            return str(next(iter(v.values())))
    return ""


def _obs_value(obs: object) -> object:
    if isinstance(obs, list):
        return obs[0] if obs else None
    return obs


def parse_sdmx_json(doc: dict) -> pd.Series:
    """Parse SDMX-JSON (2.0, with 1.0 fallbacks) into a monthly Series."""
    data = doc.get("data", doc)
    structures = data.get("structures")
    if not structures:
        single = data.get("structure") or doc.get("structure")
        structures = [single] if single else []
    datasets = data.get("dataSets") or []
    records: dict[str, object] = {}

    for ds in datasets:
        if not structures:
            break
        s_idx = ds.get("structure", 0)
        struct = structures[s_idx if isinstance(s_idx, int) and s_idx < len(structures) else 0]
        obs_dims = (struct.get("dimensions") or {}).get("observation") or []
        if not obs_dims:
            continue
        t_pos = next(
            (i for i, d in enumerate(obs_dims) if d.get("id") == "TIME_PERIOD"),
            len(obs_dims) - 1,
        )
        t_values = [_time_label(v) for v in obs_dims[t_pos].get("values", [])]

        def take(key: str, obs: object) -> None:
            parts = str(key).split(":")
            try:
                idx = int(parts[t_pos] if len(parts) > 1 else parts[0])
                records[t_values[idx]] = _obs_value(obs)
            except (ValueError, IndexError):
                pass

        for series in (ds.get("series") or {}).values():
            for k, obs in (series.get("observations") or {}).items():
                take(k, obs)
        for k, obs in (ds.get("observations") or {}).items():  # AllDimensions layout
            take(k, obs)
    return _finalise(records)


def parse_sdmx_xml(text: str) -> pd.Series:
    """Parse SDMX-ML (structure-specific or generic) into a monthly Series."""
    root = ET.fromstring(text)
    records: dict[str, object] = {}
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] != "Obs":
            continue
        t = el.attrib.get("TIME_PERIOD")
        v = el.attrib.get("OBS_VALUE")
        if t is None:  # generic layout
            for child in el:
                name = child.tag.rsplit("}", 1)[-1]
                if name in ("ObsDimension", "ObsKey") and t is None:
                    t = child.attrib.get("value")
                    if t is None:  # <ObsKey><Value id="TIME_PERIOD" value=".."/>
                        for sub in child:
                            if sub.attrib.get("id") == "TIME_PERIOD":
                                t = sub.attrib.get("value")
                elif name == "ObsValue":
                    v = child.attrib.get("value")
        if t is not None:
            records[t] = v
    return _finalise(records)


def parse_sdmx(text: str) -> pd.Series:
    body = text.lstrip("﻿ \r\n\t")
    if body.startswith("{"):
        return parse_sdmx_json(json.loads(body))
    if body.startswith("<"):
        return parse_sdmx_xml(body)
    raise ValueError("Response is neither SDMX-JSON nor SDMX-ML.")


def fetch_series(dataflow: str, key: str) -> FetchResult:
    """Download one IMF series, trying JSON then XML, with/without query params."""
    url = f"{API_BASE}/data/dataflow/{AGENCY}/{dataflow}/+/{key}"
    param_sets = [{"dimensionAtObservation": "TIME_PERIOD"}, {}]
    last_error = "No response."
    session = _session()

    for params in param_sets:
        for accept in ACCEPT_HEADERS:
            try:
                resp = session.get(
                    url, params=params, headers={"Accept": accept}, timeout=REQUEST_TIMEOUT
                )
            except requests.RequestException as exc:
                return FetchResult(status="error", url=url, message=f"Network error: {exc}")

            if resp.status_code == 404:
                return FetchResult(status="empty", url=url, message="No data (HTTP 404).")
            if resp.status_code in (406, 415):
                last_error = f"HTTP {resp.status_code} for Accept '{accept}'."
                continue
            if resp.status_code in (400, 422) and params:
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                break  # retry with the next (simpler) parameter set
            if resp.status_code != 200:
                return FetchResult(
                    status="error",
                    url=url,
                    message=f"HTTP {resp.status_code}: {resp.text[:300]}",
                )
            try:
                series = parse_sdmx(resp.text)
            except (ValueError, ET.ParseError, json.JSONDecodeError) as exc:
                last_error = f"Could not parse response ({accept}): {exc}"
                continue
            status = "ok" if not series.empty else "empty"
            msg = "" if not series.empty else "Response contained no monthly observations."
            return FetchResult(series=series, status=status, url=url, message=msg)

    return FetchResult(status="error", url=url, message=last_error)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _load_country_cached(iso3: str) -> dict[str, dict]:
    """Fetch CPI and USD exchange rate for one economy (cached as plain dicts)."""
    jobs = {"cpi": (CPI_FLOW, CPI_KEY.format(iso=iso3))}
    if iso3 != NUMERAIRE:
        jobs["er"] = (ER_FLOW, ER_KEY.format(iso=iso3))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {name: pool.submit(fetch_series, *args) for name, args in jobs.items()}
        out = {name: f.result() for name, f in futures.items()}
    if iso3 == NUMERAIRE:
        out["er"] = FetchResult(
            status="synthetic", message="USD per USD fixed at 1 (IMF rates are quoted per USD)."
        )
    return {name: vars(res).copy() for name, res in out.items()}


def load_country(iso3: str) -> dict[str, FetchResult]:
    return {name: FetchResult(**d) for name, d in _load_country_cached(iso3).items()}


# ---------------------------------------------------------------------------
# Economics
# ---------------------------------------------------------------------------
def build_panel(c1: dict[str, FetchResult], c2: dict[str, FetchResult]) -> pd.DataFrame:
    """Monthly panel of inputs, S(t) and RER(t) from Jan 1976 to the last observation."""
    observed = [r.series for r in (c1["cpi"], c1["er"], c2["cpi"], c2["er"]) if not r.series.empty]
    if not observed:
        return pd.DataFrame()
    last = max(s.index.max() for s in observed)
    idx = pd.period_range(START_PERIOD, last, freq="M")

    def aligned(res: FetchResult) -> pd.Series:
        if res.status == "synthetic":
            return pd.Series(1.0, index=idx)
        return res.series.reindex(idx)

    df = pd.DataFrame(index=idx)
    df.index.name = "month"
    df["lcu1_per_usd"] = aligned(c1["er"])
    df["lcu2_per_usd"] = aligned(c2["er"])
    df["cpi1"] = aligned(c1["cpi"])
    df["cpi2"] = aligned(c2["cpi"])

    positive = df[["lcu1_per_usd", "lcu2_per_usd", "cpi1", "cpi2"]].gt(0)
    fx_ok = positive["lcu1_per_usd"] & positive["lcu2_per_usd"]
    df["S"] = np.where(fx_ok, df["lcu2_per_usd"] / df["lcu1_per_usd"], np.nan)
    df["valid"] = positive.all(axis=1)
    df["RER"] = np.where(df["valid"], df["S"] * df["cpi1"] / df["cpi2"], np.nan)

    # Trim leading/trailing months where nothing usable exists.
    has_any = df[["S", "cpi1", "cpi2"]].notna().any(axis=1)
    if has_any.any():
        df = df.loc[has_any.idxmax(): has_any[::-1].idxmax()]
    return df


def window_stats(panel: pd.DataFrame, start: pd.Period, end: pd.Period) -> tuple[pd.DataFrame, float]:
    """Slice the panel and compute RER* and the Real Index for the window."""
    w = panel.loc[start:end].copy()
    rer_star = float(w.loc[w["valid"], "RER"].mean()) if w["valid"].any() else float("nan")
    w["RER_star"] = rer_star
    w["real_index"] = 100.0 * w["RER"] / rer_star
    return w, rer_star


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------
def label(iso3: str) -> str:
    name = COUNTRIES.get(iso3, (iso3, ""))[0]
    return f"{name} ({iso3})"


def currency(iso3: str) -> str:
    return COUNTRIES.get(iso3, (iso3, f"{iso3} currency"))[1]


def make_chart(
    w: pd.DataFrame, view: str, iso1: str, iso2: str, log_nominal: bool
) -> go.Figure:
    x = w.index.to_timestamp()
    cur1, cur2 = currency(iso1), currency(iso2)
    nominal_title = f"S(t): {cur2} per 1 {cur1}"
    real_trace = go.Scatter(
        x=x, y=w["real_index"], name="Real Index (RER* = 100)", mode="lines",
        line=dict(color="#1f5fa8", width=2), connectgaps=False,
        hovertemplate="%{x|%b %Y}<br>Real Index: %{y:.2f}<extra></extra>",
    )
    nominal_trace = go.Scatter(
        x=x, y=w["S"], name=f"Nominal S(t) ({cur2}/{cur1})", mode="lines",
        line=dict(color="#d9822b", width=2), connectgaps=False,
        hovertemplate="%{x|%b %Y}<br>S: %{y:,.6g}<extra></extra>",
    )
    y_type = "log" if log_nominal else "linear"

    if view == "Real":
        fig = go.Figure(real_trace)
        fig.add_hline(y=100, line_dash="dot", line_color="gray")
        fig.update_yaxes(title_text="Real Index (100 = sample average)")
    elif view == "Nominal":
        fig = go.Figure(nominal_trace)
        fig.update_yaxes(title_text=nominal_title, type=y_type)
    else:
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        fig.add_trace(real_trace, secondary_y=False)
        fig.add_trace(nominal_trace, secondary_y=True)
        fig.add_hline(y=100, line_dash="dot", line_color="gray", secondary_y=False)
        fig.update_yaxes(title_text="Real Index (100 = sample average)", secondary_y=False,
                         title_font_color="#1f5fa8")
        fig.update_yaxes(title_text=nominal_title, secondary_y=True, type=y_type,
                         title_font_color="#d9822b", showgrid=False)

    fig.update_layout(
        title=f"{label(iso1)} vs {label(iso2)}",
        hovermode="x unified",
        height=520,
        margin=dict(l=10, r=10, t=60, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="right", x=1),
    )
    fig.update_xaxes(title_text="Month", rangeslider_visible=False)
    return fig


def coverage_table(
    results: dict[str, FetchResult], window: pd.DataFrame
) -> pd.DataFrame:
    rows = []
    col_map = {
        "Country 1 CPI": "cpi1", "Country 1 per USD": "lcu1_per_usd",
        "Country 2 CPI": "cpi2", "Country 2 per USD": "lcu2_per_usd",
    }
    for name, res in results.items():
        s = res.series
        col = col_map[name]
        rows.append(
            {
                "series": name,
                "status": res.status,
                "first obs": str(s.index.min()) if not s.empty else "—",
                "last obs": str(s.index.max()) if not s.empty else "—",
                "obs since 1976": int(len(s)) if res.status != "synthetic" else None,
                "missing in window": int(window[col].isna().sum()) if not window.empty else None,
                "note": res.message,
            }
        )
    return pd.DataFrame(rows)


def export_frame(w: pd.DataFrame, iso1: str, iso2: str) -> pd.DataFrame:
    out = w.reset_index()
    out["month"] = out["month"].astype(str)
    out.insert(1, "country1", iso1)
    out.insert(2, "country2", iso2)
    return out.rename(
        columns={
            "lcu1_per_usd": f"country1_currency_per_USD ({currency(iso1)})",
            "lcu2_per_usd": f"country2_currency_per_USD ({currency(iso2)})",
            "S": f"S_country2_per_country1 ({currency(iso2)} per {currency(iso1)})",
            "cpi1": "P1_cpi_country1",
            "cpi2": "P2_cpi_country2",
            "valid": "valid_month",
            "RER": "RER = S*P1/P2",
            "RER_star": "RER_star (window mean)",
            "real_index": "real_index = 100*RER/RER_star",
        }
    )[
        [
            "month", "country1", "country2",
            f"country1_currency_per_USD ({currency(iso1)})",
            f"country2_currency_per_USD ({currency(iso2)})",
            f"S_country2_per_country1 ({currency(iso2)} per {currency(iso1)})",
            "P1_cpi_country1", "P2_cpi_country2", "valid_month",
            "RER = S*P1/P2", "RER_star (window mean)", "real_index = 100*RER/RER_star",
        ]
    ]


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
def country_picker(slot: str, default: str) -> str:
    codes = list(COUNTRIES)
    choice = st.sidebar.selectbox(
        f"Country {slot}",
        codes,
        index=codes.index(default),
        format_func=label,
        key=f"country_{slot}",
    )
    return choice


def main() -> None:
    st.set_page_config(page_title="Real Exchange Rate Explorer", page_icon="💱", layout="wide")
    st.title("Real Exchange Rate Explorer")
    st.caption(
        "Monthly IMF data (SDMX 3.0) from January 1976 where available. "
        "S(t) = Country 2 currency per 1 unit of Country 1 currency; "
        "RER(t) = S(t)·P1(t)/P2(t); Real Index(t) = 100·RER(t)/RER*, "
        "where RER* is the mean RER over valid months in the selected window."
    )

    st.sidebar.header("Countries")
    iso1 = country_picker("1", "USA")
    iso2 = country_picker("2", "JPN")
    with st.sidebar.expander("Use another IMF country code"):
        st.write("Overrides the selection above, e.g. `XKX`. Leave blank to ignore.")
        custom1 = st.text_input("Country 1 code", key="custom1").strip().upper()
        custom2 = st.text_input("Country 2 code", key="custom2").strip().upper()
    for custom, slot in ((custom1, 1), (custom2, 2)):
        if custom:
            if re.fullmatch(r"[A-Z0-9]{3,4}", custom):
                if slot == 1:
                    iso1 = custom
                else:
                    iso2 = custom
            else:
                st.sidebar.warning(f"Ignoring invalid code '{custom}'.")

    st.sidebar.header("Chart")
    view = st.sidebar.radio("Series to plot", ["Real", "Nominal", "Both"], index=2, horizontal=True)
    log_nominal = st.sidebar.checkbox(
        "Log scale for nominal S(t)", value=False,
        help="Useful for high-inflation currencies. Values are still actual currency units.",
    )
    if st.sidebar.button("Clear cached IMF data"):
        _load_country_cached.clear()

    if iso1 == iso2:
        st.warning("Choose two different economies: with the same country, S(t) = 1 and RER(t) = 1.")
        st.stop()

    with st.spinner(f"Fetching IMF data for {label(iso1)} and {label(iso2)}…"):
        d1 = load_country(iso1)
        d2 = load_country(iso2)

    results = {
        "Country 1 CPI": d1["cpi"], "Country 1 per USD": d1["er"],
        "Country 2 CPI": d2["cpi"], "Country 2 per USD": d2["er"],
    }
    errors = {k: v for k, v in results.items() if v.status == "error"}
    for name, res in errors.items():
        st.error(f"{name}: {res.message}\n\nRequest: `{res.url}`")

    panel = build_panel(d1, d2)
    if panel.empty or not panel["valid"].any():
        st.error(
            "No month has all four inputs (both CPIs and both USD exchange rates). "
            "The IMF may not publish monthly data for one of these economies — see coverage below."
        )
        empty = panel if not panel.empty else pd.DataFrame(
            columns=["cpi1", "cpi2", "lcu1_per_usd", "lcu2_per_usd"]
        )
        st.dataframe(coverage_table(results, empty), hide_index=True, width="stretch")
        st.stop()

    valid_months = panel.index[panel["valid"]]
    first_valid, last_valid = valid_months.min(), valid_months.max()
    options = [str(p) for p in pd.period_range(first_valid, last_valid, freq="M")]

    st.subheader("Sample period")
    start_s, end_s = st.select_slider(
        "Start and end month (RER* is recomputed for this window)",
        options=options,
        value=(options[0], options[-1]),
        key=f"window_{iso1}_{iso2}",
    )
    start, end = pd.Period(start_s, freq="M"), pd.Period(end_s, freq="M")
    window, rer_star = window_stats(panel, start, end)
    n_valid = int(window["valid"].sum())
    n_total = len(window)

    if n_valid == 0:
        st.warning("No valid months in this window; widen the date range.")
        st.stop()

    latest = window.loc[window["valid"]].iloc[-1]
    latest_month = window.index[window["valid"]][-1]
    cur1, cur2 = currency(iso1), currency(iso2)

    c = st.columns(4)
    c[0].metric("RER* (window mean)", f"{rer_star:,.6g}")
    c[1].metric(
        f"Real Index, {latest_month.strftime('%b %Y')}",
        f"{latest['real_index']:.1f}",
        f"{latest['real_index'] - 100:+.1f} vs average",
        delta_color="off",
    )
    c[2].metric(f"S(t), {latest_month.strftime('%b %Y')}", f"{latest['S']:,.6g}",
                help=f"{cur2} per 1 {cur1}")
    c[3].metric("Valid months", f"{n_valid} / {n_total}")

    if n_valid < n_total:
        st.info(
            f"{n_total - n_valid} month(s) in the window lack one or more inputs; they are "
            "excluded from RER* and shown as gaps in the charts."
        )

    st.plotly_chart(make_chart(window, view, iso1, iso2, log_nominal), width="stretch")
    st.caption(
        f"A rising Real Index means {label(iso1)} goods become more expensive relative to "
        f"{label(iso2)} goods (real appreciation of Country 1's currency). "
        f"Nominal S(t) is plotted in actual units ({cur2} per {cur1}), not normalised."
    )

    export = export_frame(window, iso1, iso2)
    st.download_button(
        "Download CSV (selected sample period)",
        data=export.to_csv(index=False).encode("utf-8"),
        file_name=f"rer_{iso1}_{iso2}_{start}_{end}.csv",
        mime="text/csv",
        type="primary",
    )

    with st.expander("Data coverage and gaps"):
        st.dataframe(coverage_table(results, window), hide_index=True, width="stretch")
        st.caption(
            "Exchange rates: IMF ER dataflow, key {ISO3}.XDC_USD.PA_RT.M (period-average, "
            "national currency per USD). CPI: IMF CPI dataflow, key {ISO3}.CPI._T.IX.M. "
            "Euro area code: G163."
        )
        for name, res in results.items():
            if res.url:
                st.write(f"**{name}:** `{res.url}`")

    with st.expander("Observations in window"):
        st.dataframe(export, hide_index=True, width="stretch")


if __name__ == "__main__":
    main()
