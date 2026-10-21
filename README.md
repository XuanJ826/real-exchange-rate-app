# Real Exchange Rate Explorer

A Streamlit app that compares monthly nominal and real exchange rates using IMF CPI and exchange-rate series. It uses Python 3.12, the IMF SDMX 3.0 API, pandas, and Plotly.

## Files

- **app.py** — Streamlit application
- **requirements.txt** — Python dependencies

## Data and calculations

The app reads the IMF country code list from:

https://api.imf.org/external/sdmx/3.0/structure/codelist/IMF/CL_COUNTRY/+?format=sdmx-json

For each selected country, it requests the latest version of these IMF.STA dataflows:

- CPI: {country}.CPI._T.IX.M
- Exchange rate: {country}.XDC_USD.PA_RT.M

The request uses the IMF SDMX 3.0 data endpoint and SDMX-JSON 2.0 response format. The parser reads the TIME_PERIOD observation dimension and maps each indexed observation to its month. The app caches country lists for 24 hours and each country/series response for 24 hours.

The app displays January 1976 onward. It filters earlier IMF observations locally because the live API response tested during development returned the full series even when startPeriod and endPeriod were supplied. Months without data stay blank; the app does not interpolate or carry values forward.

The calculations are:

1. Nominal S = ER2 / ER1, which gives Country 2 currency per one unit of Country 1 currency.
2. RER = Nominal S × CPI1 / CPI2.
3. RER* is the arithmetic mean of valid monthly RER values inside the selected date range.
4. Real Index = 100 × RER / RER*.

Only months with positive, finite values for both CPI observations and both exchange-rate observations enter the RER average and Real Index. The nominal rate is shown in its actual currency units and is not normalized. The CSV includes all four source observations, the IMF series keys, and the resulting calculations.

The IMF country list includes Euro Area G163, which is available in the selector as requested. In the live API check during development, the CPI request for G163.CPI._T.IX.M returned no observations. The app keeps that selection available and reports missing IMF coverage without crashing; a real index cannot be calculated until valid observations are returned.

## Run locally

Install Python 3.12, then run these commands from the directory containing the files:

~~~bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
streamlit run app.py
~~~

On Windows PowerShell, activate the environment with:

~~~powershell
.venv\Scripts\Activate.ps1
~~~

The app opens in a browser. The default comparison is the United Kingdom (GBR) as Country 1 and the United States (USA) as Country 2. In that order, the nominal rate is USD per GBP.

## Deploy from GitHub to Streamlit Community Cloud

1. Create a GitHub repository and add app.py, requirements.txt, and README.md at the repository root.
2. Push the files to the repository's default branch.
3. Sign in to Streamlit Community Cloud and choose **Create app**.
4. Select the repository and branch, then set the main file path to app.py.
5. In the app's advanced settings, select **Python 3.12**, then deploy.

No API secret is needed for the IMF endpoints verified by this app. Community Cloud must be able to make outbound HTTPS requests to api.imf.org.

## Live API and calculation checks

During development, live IMF requests for the U.S. and U.K. CPI keys and the U.S. exchange-rate key returned SDMX-JSON data. The CPI response included TIME_PERIOD values such as 1955-M01; observations were supplied in an index-keyed map. The app's parser follows that response shape. Live coverage can change, and the app handles empty series, HTTP errors, and malformed responses with clear messages.

The calculation is structured so a selected-date change recomputes RER* from only valid months in that selected window. The CSV keeps full-precision values rather than rounding calculations before export, so the mean of exported nonblank Real Index values is 100 up to floating-point rounding.
