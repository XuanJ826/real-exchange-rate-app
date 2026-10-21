from __future__ import annotations

import math
import re
from typing import Any

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import pycountry
import requests
import streamlit as st


IMF_BASE_URL = "https://api.imf.org/external/sdmx/3.0"
MIN_PERIOD = pd.Period("1976-01", freq="M")
REQUEST_TIMEOUT = (8, 45)
DATA_HEADERS = {
    "Accept": "application/vnd.sdmx.data+json;version=2.0.0",
    "User-Agent": "real-exchange-rate-streamlit/1.0",
}
STRUCTURE_HEADERS = {
    "Accept": "application/vnd.sdmx.structure+json;version=2.0.0",
    "User-Agent": "real-exchange-rate-streamlit/1.0",
}


class IMFAPIError(RuntimeError):
    """Raised when the IMF API request or its response cannot be used."""


def _empty_series() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "period": pd.PeriodIndex([], freq="M"),
            "value": pd.Series(dtype="float64"),
        }
    )


def _parse_month(value: Any) -> pd.Period | None:
    """Parse IMF SDMX periods such as 2026-M09 and ordinary YYYY-MM values."""
    if value is None:
        return None
    text = str(value).strip()
    match = re.fullmatch(r"(\d{4})-M(\d{1,2})", text)
    if match:
        text = f"{match.group(1)}-{int(match.group(2)):02d}"
    try:
        return pd.Period(text, freq="M")
    except (TypeError, ValueError):
        return None


def _number_or_nan(value: Any) -> float:
    if value is None:
        return math.nan
    try:
        number = float(value)
    except (TypeError, ValueError):
        return math.nan
    return number if math.isfinite(number) else math.nan


def parse_sdmx_observations(payload: dict[str, Any]) -> pd.DataFrame:
    """Decode the IMF SDMX-JSON 2.0 indexed observation representation."""
    data = payload.get("data", payload)
    structures = data.get("structures") or []
    datasets = data.get("dataSets") or []
    if not structures or not datasets:
        return _empty_series()

    dimensions = structures[0].get("dimensions") or {}
    observation_dimensions = dimensions.get("observation") or []
    time_dimension = next(
        (dimension for dimension in observation_dimensions
         if dimension.get("id") == "TIME_PERIOD"),
        None,
    )
    if time_dimension is None:
        raise IMFAPIError("The IMF response did not include a TIME_PERIOD dimension.")

    time_values = [
        (item or {}).get("value")
        for item in (time_dimension.get("values") or [])
    ]
    parsed: list[tuple[pd.Period, float]] = []

    def add_observations(observations: dict[str, Any]) -> None:
        for observation_key, observation in observations.items():
            try:
                # In SDMX-JSON, observation keys are zero-based dimension indexes.
                index = int(str(observation_key).split(":")[-1])
                period = _parse_month(time_values[index])
            except (IndexError, TypeError, ValueError):
                continue
            if period is None or period < MIN_PERIOD:
                continue
            raw_value = observation[0] if isinstance(observation, (list, tuple)) else observation
            parsed.append((period, _number_or_nan(raw_value)))

    for dataset in datasets:
        series = dataset.get("series") or {}
        if series:
            for series_item in series.values():
                add_observations(series_item.get("observations") or {})
        else:
            # Support data messages that put observations directly on the dataset.
            add_observations(dataset.get("observations") or {})

    if not parsed:
        return _empty_series()

    result = pd.DataFrame(parsed, columns=["period", "value"])
    result = result.sort_values("period").drop_duplicates("period", keep="first")
    return result.reset_index(drop=True)


@st.cache_data(ttl=24 * 60 * 60, show_spinner=False)
def load_imf_country_names() -> dict[str, str]:
    """Load IMF country/area labels, retaining ISO alpha-3 codes and Euro Area."""
    url = f"{IMF_BASE_URL}/structure/codelist/IMF/CL_COUNTRY/+"
    response = requests.get(
        url,
        params={"format": "sdmx-json"},
        headers=STRUCTURE_HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code == 204:
        raise IMFAPIError("The IMF country code list was empty.")
    if response.status_code >= 400:
        raise IMFAPIError(f"IMF country list request returned HTTP {response.status_code}.")
    try:
        codelists = response.json().get("data", {}).get("codelists", [])
    except ValueError as exc:
        raise IMFAPIError("The IMF country code list was not valid JSON.") from exc
    if not codelists:
        raise IMFAPIError("The IMF response did not contain a country code list.")

    names: dict[str, str] = {}
    for item in codelists[0].get("codes", []):
        code = str(item.get("id", "")).upper()
        if not code:
            continue
        # CL_COUNTRY also contains regional aggregates. Keep actual ISO countries
        # and the IMF Euro Area code explicitly requested for this app.
        is_iso_country = (
            len(code) == 3
            and code.isalpha()
            and pycountry.countries.get(alpha_3=code) is not None
        )
        if code == "G163" or is_iso_country:
            localized = item.get("names", {}).get("en")
            name = localized or item.get("name") or code
            names[code] = str(name)

    names.setdefault("G163", "Euro Area (G163)")
    if not names:
        raise IMFAPIError("No country entries were found in the IMF code list.")
    return names


def fallback_country_names() -> dict[str, str]:
    """Provide a usable country picker if the IMF code list is temporarily down."""
    names = {country.alpha_3: country.name for country in pycountry.countries}
    names["G163"] = "Euro Area (G163)"
    return names


@st.cache_data(ttl=24 * 60 * 60, show_spinner=False)
def load_imf_series(country_code: str, dataflow: str) -> pd.DataFrame:
    """Fetch and decode one monthly IMF CPI or exchange-rate series."""
    if dataflow == "CPI":
        series_key = f"{country_code}.CPI._T.IX.M"
    elif dataflow == "ER":
        series_key = f"{country_code}.XDC_USD.PA_RT.M"
    else:
        raise ValueError(f"Unsupported IMF dataflow: {dataflow}")

    url = f"{IMF_BASE_URL}/data/dataflow/IMF.STA/{dataflow}/+/{series_key}"
    response = requests.get(
        url,
        params={"format": "jsondata"},
        headers=DATA_HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code == 204:
        return _empty_series()
    if response.status_code >= 400:
        detail = response.text[:250].replace("\n", " ")
        raise IMFAPIError(
            f"{dataflow} request for {country_code} returned HTTP "
            f"{response.status_code}. {detail}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise IMFAPIError(f"{dataflow} response for {country_code} was not valid JSON.") from exc

    return parse_sdmx_observations(payload)


def align_monthly_series(
    series_map: dict[str, pd.DataFrame],
    end_period: pd.Period,
) -> pd.DataFrame:
    """Create a full monthly calendar from Jan 1976, leaving missing values blank."""
    calendar = pd.period_range(MIN_PERIOD, end_period, freq="M")
    aligned = pd.DataFrame(index=calendar)
    aligned.index.name = "period"

    for column, observations in series_map.items():
        if observations.empty:
            aligned[column] = np.nan
            continue
        values = observations.drop_duplicates("period", keep="first").set_index("period")["value"]
        aligned[column] = values.reindex(calendar)
    return aligned


def calculate_rer(
    monthly: pd.DataFrame,
    start_period: pd.Period,
    end_period: pd.Period,
) -> tuple[pd.DataFrame, float, int]:
    """Calculate nominal S, RER, selected-period RER*, and the real index."""
    selected = monthly.loc[start_period:end_period].copy()
    required_inputs = ["cpi_1", "cpi_2", "er_1", "er_2"]
    input_valid = selected[required_inputs].notna().all(axis=1)
    input_valid &= (selected[required_inputs] > 0).all(axis=1)

    # IMF exchange rates are local currency per USD. Thus ER2 / ER1 is
    # Country 2 currency units per one Country 1 currency unit.
    selected["nominal_s"] = (selected["er_2"] / selected["er_1"]).replace(
        [np.inf, -np.inf], np.nan
    )
    selected["nominal_s"] = selected["nominal_s"].where(
        selected["er_1"].gt(0) & selected["er_2"].gt(0)
    )
    selected["rer"] = (selected["nominal_s"] * selected["cpi_1"] / selected["cpi_2"]).replace(
        [np.inf, -np.inf], np.nan
    )
    valid_rer = input_valid & selected["rer"].notna()
    selected["valid_rer_month"] = valid_rer

    valid_count = int(valid_rer.sum())
    rer_star = float(selected.loc[valid_rer, "rer"].mean()) if valid_count else math.nan
    selected["rer_star"] = rer_star
    if math.isfinite(rer_star) and rer_star != 0:
        selected["real_index"] = (100.0 * selected["rer"] / rer_star).where(valid_rer)
    else:
        selected["real_index"] = np.nan
    return selected, rer_star, valid_count


def build_csv_export(
    selected: pd.DataFrame,
    country_1: str,
    country_2: str,
    country_names: dict[str, str],
    start_period: pd.Period,
    end_period: pd.Period,
    rer_star: float,
) -> pd.DataFrame:
    """Build a self-contained CSV with source keys and every RER input."""
    return pd.DataFrame(
        {
            "month": [str(period) for period in selected.index],
            "country_1_name": country_names[country_1],
            "country_1_code": country_1,
            "country_1_cpi_imf_series_key": f"{country_1}.CPI._T.IX.M",
            "country_1_cpi_index": selected["cpi_1"].to_numpy(),
            "country_1_exchange_rate_imf_series_key": f"{country_1}.XDC_USD.PA_RT.M",
            "country_1_local_currency_per_usd": selected["er_1"].to_numpy(),
            "country_2_name": country_names[country_2],
            "country_2_code": country_2,
            "country_2_cpi_imf_series_key": f"{country_2}.CPI._T.IX.M",
            "country_2_cpi_index": selected["cpi_2"].to_numpy(),
            "country_2_exchange_rate_imf_series_key": f"{country_2}.XDC_USD.PA_RT.M",
            "country_2_local_currency_per_usd": selected["er_2"].to_numpy(),
            "nominal_S_country_2_currency_per_country_1_currency": selected[
                "nominal_s"
            ].to_numpy(),
            "RER_country_2_currency_per_country_1_currency": selected["rer"].to_numpy(),
            "selected_period_average_RER_star": rer_star,
            "real_index_selected_period_average_100": selected["real_index"].to_numpy(),
            "valid_RER_month": selected["valid_rer_month"].to_numpy(),
            "selected_start_month": str(start_period),
            "selected_end_month": str(end_period),
        }
    )


def _format_number(value: float) -> str:
    return f"{value:,.6g}" if math.isfinite(value) else "—"


def _month_text(period: pd.Period) -> str:
    return period.to_timestamp().strftime("%B %Y")


def make_chart(
    selected: pd.DataFrame,
    chart_mode: str,
    country_1: str,
    country_2: str,
    country_names: dict[str, str],
) -> go.Figure:
    """Create the requested Plotly chart, using a secondary axis for Both."""
    x_values = selected.index.to_timestamp()
    title = (
        f"{country_names[country_1]} ({country_1}) vs "
        f"{country_names[country_2]} ({country_2})"
    )
    figure = go.Figure()

    if chart_mode in ("Real", "Both"):
        figure.add_trace(
            go.Scatter(
                x=x_values,
                y=selected["real_index"],
                name="Real Index",
                mode="lines",
                line={"color": "#2563eb", "width": 2},
                connectgaps=False,
                hovertemplate="%{y:.2f}<extra>Real Index</extra>",
            )
        )
        figure.add_hline(
            y=100,
            line={"color": "#64748b", "width": 1, "dash": "dot"},
            annotation_text="100",
            annotation_position="top left",
        )

    if chart_mode == "Nominal":
        figure.add_trace(
            go.Scatter(
                x=x_values,
                y=selected["nominal_s"],
                name="Nominal S",
                mode="lines",
                line={"color": "#e11d48", "width": 2},
                connectgaps=False,
                hovertemplate="%{y:.6g}<extra>Nominal S</extra>",
            )
        )
    elif chart_mode == "Both":
        figure.add_trace(
            go.Scatter(
                x=x_values,
                y=selected["nominal_s"],
                name="Nominal S",
                mode="lines",
                line={"color": "#e11d48", "width": 2},
                connectgaps=False,
                yaxis="y2",
                hovertemplate="%{y:.6g}<extra>Nominal S</extra>",
            )
        )

    layout: dict[str, Any] = {
        "title": title,
        "template": "plotly_white",
        "hovermode": "x unified",
        "margin": {"l": 12, "r": 12, "t": 58, "b": 12},
        "legend": {
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.02,
            "xanchor": "right",
            "x": 1,
        },
        "xaxis": {"title": "Month", "showgrid": False},
    }
    if chart_mode == "Real":
        layout["yaxis"] = {"title": "Real Index (selected-period average = 100)"}
    elif chart_mode == "Nominal":
        layout["yaxis"] = {
            "title": f"Country 2 currency per Country 1 currency ({country_2}/{country_1})"
        }
    else:
        layout["yaxis"] = {
            "title": "Real Index (selected-period average = 100)",
            "side": "left",
        }
        layout["yaxis2"] = {
            "title": f"Nominal S ({country_2} currency / {country_1} currency)",
            "overlaying": "y",
            "side": "right",
            "showgrid": False,
        }
    figure.update_layout(**layout)
    return figure


def main() -> None:
    st.set_page_config(
        page_title="Real Exchange Rate Explorer",
        page_icon="📈",
        layout="wide",
    )
    st.title("Real Exchange Rate Explorer")
    st.write(
        "Compare monthly nominal and real exchange rates using IMF CPI and "
        "exchange-rate data."
    )

    try:
        country_names = load_imf_country_names()
    except Exception as exc:
        country_names = fallback_country_names()
        st.sidebar.warning(
            "The IMF country list could not be loaded. Showing ISO country choices "
            f"instead. Details: {exc}"
        )

    country_codes = sorted(country_names, key=lambda code: country_names[code].casefold())
    default_1 = country_codes.index("GBR") if "GBR" in country_codes else 0
    default_2 = country_codes.index("USA") if "USA" in country_codes else min(1, len(country_codes) - 1)
    st.sidebar.header("Selections")
    country_1 = st.sidebar.selectbox(
        "Country 1",
        country_codes,
        index=default_1,
        format_func=lambda code: f"{country_names[code]} ({code})",
    )
    country_2 = st.sidebar.selectbox(
        "Country 2",
        country_codes,
        index=default_2,
        format_func=lambda code: f"{country_names[code]} ({code})",
    )

    requests_to_load = {
        "cpi_1": (country_1, "CPI"),
        "cpi_2": (country_2, "CPI"),
        "er_1": (country_1, "ER"),
        "er_2": (country_2, "ER"),
    }
    series_data: dict[str, pd.DataFrame] = {}
    failures: list[str] = []
    with st.spinner("Loading monthly IMF observations..."):
        for output_name, (code, flow) in requests_to_load.items():
            try:
                series_data[output_name] = load_imf_series(code, flow)
            except Exception as exc:
                series_data[output_name] = _empty_series()
                failures.append(f"{country_names[code]} {flow}: {exc}")

    if failures:
        st.warning(
            "Some IMF requests failed. The app will continue with available data. "
            + " | ".join(failures)
        )
    for output_name, (code, flow) in requests_to_load.items():
        observations = series_data[output_name]
        if observations.empty or not observations["value"].notna().any():
            measure = "CPI" if flow == "CPI" else "exchange-rate"
            st.info(
                f"No monthly {measure} observations from January 1976 onward were "
                f"returned for {country_names[code]} ({code})."
            )

    all_observed_periods = [
        period
        for observations in series_data.values()
        for period in observations.get("period", [])
    ]
    latest_data_period = max(all_observed_periods) if all_observed_periods else None
    if latest_data_period is None or latest_data_period < MIN_PERIOD:
        latest_data_period = pd.Timestamp.today().to_period("M")
    month_options = pd.period_range(MIN_PERIOD, latest_data_period, freq="M")

    def format_month(period: pd.Period) -> str:
        return _month_text(period)

    st.sidebar.selectbox(
        "Start month",
        month_options,
        index=0,
        format_func=format_month,
        key="start_month",
    )
    st.sidebar.selectbox(
        "End month",
        month_options,
        index=len(month_options) - 1,
        format_func=format_month,
        key="end_month",
    )
    chart_mode = st.sidebar.selectbox("Display", ["Real", "Nominal", "Both"], index=2)
    start_period = st.session_state["start_month"]
    end_period = st.session_state["end_month"]
    if start_period > end_period:
        st.error("Start month must be on or before end month.")
        return

    monthly = align_monthly_series(series_data, end_period)
    selected, rer_star, valid_count = calculate_rer(monthly, start_period, end_period)

    st.caption(
        f"Selected period: {_month_text(start_period)} to {_month_text(end_period)}. "
        "Missing observations are left blank; no values are interpolated."
    )
    metrics = st.columns(4)

    latest_nominal = selected["nominal_s"].dropna()
    if latest_nominal.empty:
        nominal_value = "—"
        nominal_month = "No nominal observations in range"
    else:
        nominal_value = _format_number(float(latest_nominal.iloc[-1]))
        nominal_month = _month_text(latest_nominal.index[-1])
    metrics[0].metric("Latest nominal exchange rate", nominal_value)
    metrics[0].caption(
        f"{nominal_month} · Country 2 currency per Country 1 currency"
    )

    latest_real = selected["real_index"].dropna()
    if latest_real.empty:
        real_value = "—"
        real_month = "No valid RER observations in range"
    else:
        real_value = f"{float(latest_real.iloc[-1]):.2f}"
        real_month = _month_text(latest_real.index[-1])
    metrics[1].metric("Latest Real Index", real_value)
    metrics[1].caption(real_month)

    metrics[2].metric("Average RER (RER*)", _format_number(rer_star))
    metrics[2].caption("Arithmetic mean of valid monthly RER values")
    metrics[3].metric("Valid RER months", f"{valid_count:,}")
    metrics[3].caption("All four CPI and exchange-rate inputs present")

    if valid_count:
        mean_index = float(selected.loc[selected["valid_rer_month"], "real_index"].mean())
        st.caption(f"Mean Real Index across valid months: {mean_index:.12f}")
    else:
        st.info(
            "There are no months with all four valid inputs in this date range, "
            "so RER* and the Real Index cannot be calculated."
        )

    st.plotly_chart(
        make_chart(selected, chart_mode, country_1, country_2, country_names),
        width="stretch",
    )

    table = pd.DataFrame(
        {
            "Month": [str(period) for period in selected.index],
            f"Country 1 CPI ({country_1})": selected["cpi_1"].to_numpy(),
            f"Country 2 CPI ({country_2})": selected["cpi_2"].to_numpy(),
            f"Country 1 local currency per USD ({country_1})": selected[
                "er_1"
            ].to_numpy(),
            f"Country 2 local currency per USD ({country_2})": selected[
                "er_2"
            ].to_numpy(),
            "Nominal S (Country 2 currency / Country 1 currency)": selected[
                "nominal_s"
            ].to_numpy(),
            "RER": selected["rer"].to_numpy(),
            "RER* (selected-period average)": selected["rer_star"].to_numpy(),
            "Real Index": selected["real_index"].to_numpy(),
            "Valid RER month": selected["valid_rer_month"].to_numpy(),
        }
    )
    st.subheader("Monthly observations and calculations")
    st.dataframe(table, width="stretch", hide_index=True)
    st.caption(
        "The CSV includes both CPI values, both local-currency-per-USD inputs, "
        "nominal S, RER, RER*, and the Real Index for every month in the selected range."
    )

    export = build_csv_export(
        selected,
        country_1,
        country_2,
        country_names,
        start_period,
        end_period,
        rer_star,
    )
    filename = f"rer_{country_1}_{country_2}_{start_period}_{end_period}.csv"
    st.download_button(
        "Download selected period as CSV",
        data=export.to_csv(index=False, na_rep="").encode("utf-8"),
        file_name=filename,
        mime="text/csv",
    )

    with st.expander("How the real exchange rate is calculated"):
        st.markdown(
            """
            - **Nominal S(t)** = ER₂(t) / ER₁(t), where each IMF exchange rate is local currency per USD. S is therefore Country 2 currency units per one unit of Country 1 currency. For the UK (GBR) as Country 1 and the U.S. (USA) as Country 2, S is **USD per GBP**.
            - **RER(t)** = S(t) × CPI₁(t) / CPI₂(t).
            - **RER*** is the arithmetic average of valid monthly RER values inside the selected date range.
            - **Real Index(t)** = 100 × RER(t) / RER*. Its average across valid months in the selected range is 100.

            A month enters the RER average and Real Index only when both CPI values and both exchange rates are present, finite, and positive. The nominal exchange rate only needs both exchange-rate inputs and remains in actual currency units; it is not normalized.
            """
        )


if __name__ == "__main__":
    main()
