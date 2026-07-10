#!/usr/bin/env python3
"""
Morning briefing data fetcher.

Runs on GitHub Actions (open network), writes data/briefing.json.
Chat then reads it via:
  https://raw.githubusercontent.com/yutsutke/<repo>/main/data/briefing.json

Data sources (weather = keyless; stocks = yfinance keyless, movers = optional FMP key):
  - Open-Meteo   : numeric daily forecast for Hachioji
  - JMA (気象庁)  : official Tokyo-area weather text
  - yfinance     : index levels + watchlist quotes
  - FMP (optional): market gainers/losers, only if FMP_API_KEY secret is set

Design notes:
  - Fetch EVERYTHING every day. Day-of-week rules (skip Mon stock ranking,
    add weekend weather Wed/Thu/Fri) are a *presentation* concern handled in chat,
    not here. The JSON is a superset.
  - Every source is wrapped in try/except; failures are recorded in "errors"
    so the JSON always writes.
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta

import requests  # top-level: small, always available on the runner

JST = timezone(timedelta(hours=9))

# ---- Location (Hachioji) -------------------------------------------------
LAT, LON = 35.66, 139.32
LOCATION_NAME = "八王子"

# ---- Watchlist (edit freely) --------------------------------------------
INDICES = ["^GSPC", "^IXIC", "^DJI"]
SPACE = ["RDW", "LUNR", "RKLB", "ASTS"]
AI_SEMI = ["NVDA", "MU", "AVGO", "TSM"]

NAMES = {
    "^GSPC": "S&P 500", "^IXIC": "NASDAQ総合", "^DJI": "ダウ工業株30種",
    "RDW": "Redwire", "LUNR": "Intuitive Machines", "RKLB": "Rocket Lab",
    "ASTS": "AST SpaceMobile", "NVDA": "NVIDIA", "MU": "Micron",
    "AVGO": "Broadcom", "TSM": "TSMC",
}

# ---- Weather code map (WMO -> Japanese) ---------------------------------
WMO = {
    0: "快晴", 1: "晴れ", 2: "晴れ時々曇り", 3: "曇り",
    45: "霧", 48: "着氷性の霧",
    51: "弱い霧雨", 53: "霧雨", 55: "強い霧雨",
    56: "弱い着氷性霧雨", 57: "着氷性霧雨",
    61: "弱い雨", 63: "雨", 65: "強い雨",
    66: "弱い着氷性の雨", 67: "着氷性の雨",
    71: "弱い雪", 73: "雪", 75: "強い雪", 77: "霧雪",
    80: "にわか雨", 81: "やや強いにわか雨", 82: "激しいにわか雨",
    85: "にわか雪", 86: "強いにわか雪",
    95: "雷雨", 96: "雷を伴うひょう", 99: "激しい雷を伴うひょう",
}
WD = ["月", "火", "水", "木", "金", "土", "日"]


# ---- Pure transforms (unit-testable, no network) ------------------------
def parse_open_meteo(data):
    d = data["daily"]
    out = []
    for i, day in enumerate(d["time"]):
        dt = datetime.strptime(day, "%Y-%m-%d")
        code = d["weathercode"][i]
        out.append({
            "date": day,
            "wday": WD[dt.weekday()],
            "tmax": d["temperature_2m_max"][i],
            "tmin": d["temperature_2m_min"][i],
            "pop": d["precipitation_probability_max"][i],
            "code": code,
            "label": WMO.get(code, f"code{code}"),
        })
    return out


def parse_jma_tokyo_text(data, area_name="東京地方"):
    """Extract the official forecast text for a JMA sub-area (default 東京地方)."""
    ts = data[0]["timeSeries"]
    for series in ts:
        defines = series.get("timeDefines", [])
        for area in series.get("areas", []):
            if area.get("area", {}).get("name") == area_name and "weathers" in area:
                res = []
                for j, w in enumerate(area["weathers"]):
                    date = defines[j][:10] if j < len(defines) else ""
                    # JMA separates tokens with U+3000; collapse all whitespace
                    res.append({"date": date, "text": "".join(w.split())})
                return res
    return []


# ---- Network fetchers ----------------------------------------------------
def fetch_weather():
    om = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": LAT, "longitude": LON,
            "daily": "temperature_2m_max,temperature_2m_min,"
                     "precipitation_probability_max,weathercode",
            "timezone": "Asia/Tokyo",
            "forecast_days": 7,
        },
        timeout=20,
    )
    om.raise_for_status()
    daily = parse_open_meteo(om.json())

    jma_text = []
    try:
        j = requests.get(
            "https://www.jma.go.jp/bosai/forecast/data/forecast/130000.json",
            timeout=20,
        )
        j.raise_for_status()
        jma_text = parse_jma_tokyo_text(j.json())
    except Exception as e:  # JMA text is a nice-to-have; don't fail weather over it
        jma_text = [{"error": f"jma: {e}"}]

    return {"source": "open-meteo + JMA", "daily": daily, "jma_text_tokyo": jma_text}


def fetch_quotes(tickers):
    import yfinance as yf  # lazy import so the module loads without yfinance installed
    out = []
    for t in tickers:
        try:
            h = yf.Ticker(t).history(period="7d", interval="1d")
            closes = h["Close"].dropna()
            price = float(closes.iloc[-1])
            prev = float(closes.iloc[-2])
            chg = price - prev
            pct = (chg / prev * 100) if prev else 0.0
            out.append({
                "ticker": t, "name": NAMES.get(t, t),
                "price": round(price, 2), "prev_close": round(prev, 2),
                "change": round(chg, 2), "pct": round(pct, 2),
            })
        except Exception as e:
            out.append({"ticker": t, "name": NAMES.get(t, t), "error": str(e)})
    return out


def fetch_movers_fmp(key, n=3):
    base = "https://financialmodelingprep.com/api/v3/stock_market"

    def top(url):
        r = requests.get(url, params={"apikey": key}, timeout=20)
        r.raise_for_status()
        rows = r.json()
        return [{
            "ticker": i.get("symbol"), "name": i.get("name"),
            "price": i.get("price"), "pct": i.get("changesPercentage"),
        } for i in rows[:n]]

    return {"gainers": top(f"{base}/gainers"), "losers": top(f"{base}/losers")}


# ---- Main ----------------------------------------------------------------
def main():
    now = datetime.now(JST)
    errors = []

    weather = None
    try:
        weather = fetch_weather()
    except Exception as e:
        errors.append(f"weather: {e}")

    indices = watchlist = None
    try:
        indices = fetch_quotes(INDICES)
        watchlist = {"space": fetch_quotes(SPACE), "ai_semi": fetch_quotes(AI_SEMI)}
    except Exception as e:
        errors.append(f"quotes: {e}")

    movers = None
    fmp_key = os.environ.get("FMP_API_KEY")
    if fmp_key:
        try:
            movers = fetch_movers_fmp(fmp_key)
        except Exception as e:
            errors.append(f"movers: {e}")

    payload = {
        "generated_at_jst": now.isoformat(timespec="seconds"),
        "generated_at_utc": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "location": {"name": LOCATION_NAME, "lat": LAT, "lon": LON},
        "weather": weather,
        "markets": {"indices": indices, "watchlist": watchlist, "movers": movers},
        "errors": errors,
    }

    os.makedirs("data", exist_ok=True)
    with open("data/briefing.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"Wrote data/briefing.json (errors: {len(errors)})")
    for e in errors:
        print("  -", e)


if __name__ == "__main__":
    main()
