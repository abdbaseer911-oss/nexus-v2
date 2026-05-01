import os
import httpx
import json
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from dotenv import load_dotenv
import google.generativeai as genai

load_dotenv()

# Securely retrieve the keys from the environment
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")

if GEMINI_KEY:
    genai.configure(api_key=GEMINI_KEY)

app = FastAPI(title="NEXUS API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FH_BASE = "https://finnhub.io/api/v1"

# ── helpers ───────────────────────────────────────────────────────────────────

async def fh_get(path: str) -> dict:
    if not FINNHUB_KEY:
        raise HTTPException(503, "FINNHUB_API_KEY not configured")
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{FH_BASE}{path}&token={FINNHUB_KEY}")
        r.raise_for_status()
        return r.json()

def fmt_big(v):
    if v is None: return "—"
    v = float(v)
    if v >= 1e12: return f"${v/1e12:.2f}T"
    if v >= 1e9:  return f"${v/1e9:.2f}B"
    if v >= 1e6:  return f"${v/1e6:.2f}M"
    return f"${v:,.0f}"

def fmt_vol(v):
    if v is None: return "—"
    v = float(v)
    if v >= 1e9: return f"{v/1e9:.2f}B"
    if v >= 1e6: return f"{v/1e6:.1f}M"
    if v >= 1e3: return f"{v/1e3:.0f}K"
    return str(int(v))

# ── endpoints ─────────────────────────────────────────────────────────────────

@app.get("/api/quote/{ticker}")
async def get_quote(ticker: str):
    ticker = ticker.upper().strip()
    try:
        quote   = await fh_get(f"/quote?symbol={ticker}")
        profile = await fh_get(f"/stock/profile2?symbol={ticker}")
        metrics = await fh_get(f"/stock/metric?symbol={ticker}&metric=all")
        m = metrics.get("metric", {})
        return {
            "symbol":       ticker,
            "name":         profile.get("name", ticker),
            "sector":       profile.get("finnhubIndustry", "—"),
            "exchange":     profile.get("exchange", "—"),
            "currency":     profile.get("currency", "USD"),
            "logo":         profile.get("logo", ""),
            "weburl":       profile.get("weburl", ""),
            "price":        quote.get("c"),
            "change":       quote.get("d"),
            "changePct":    quote.get("dp"),
            "high":         quote.get("h"),
            "low":          quote.get("l"),
            "open":         quote.get("o"),
            "prevClose":    quote.get("pc"),
            "marketCap":    fmt_big(profile.get("marketCapitalization", 0) * 1e6 if profile.get("marketCapitalization") else None),
            "marketCapRaw": (profile.get("marketCapitalization") or 0) * 1e6,
            "peRatio":      round(m.get("peNormalizedAnnual", 0) or 0, 2),
            "eps":          round(m.get("epsNormalizedAnnual", 0) or 0, 2),
            "beta":         round(m.get("beta", 0) or 0, 2),
            "dividendYield":round(m.get("dividendYieldIndicatedAnnual", 0) or 0, 2),
            "week52High":   m.get("52WeekHigh"),
            "week52Low":    m.get("52WeekLow"),
            "avgVolume":    fmt_vol(m.get("averageDailyVolume10Day")),
            "shareFloat":   fmt_big(m.get("shareFloat")),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/news/{ticker}")
async def get_news(ticker: str):
    from datetime import date, timedelta
    ticker = ticker.upper().strip()
    try:
        to_date   = date.today().isoformat()
        from_date = (date.today() - timedelta(days=7)).isoformat()
        data = await fh_get(f"/company-news?symbol={ticker}&from={from_date}&to={to_date}")
        if not isinstance(data, list):
            return {"news": []}
        items = data[:15]
        return {"news": [
            {
                "headline": n.get("headline", ""),
                "source":   n.get("source", ""),
                "url":      n.get("url", ""),
                "datetime": n.get("datetime", 0),
                "summary":  n.get("summary", "")[:200],
            }
            for n in items
        ]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/candles/{ticker}")
async def get_candles(ticker: str, range: str = "1mo"):
    import time
    ticker = ticker.upper().strip()
    now = int(time.time())
    range_map = {
        "1d":  (now - 86400,    "15"),
        "5d":  (now - 432000,   "60"),
        "1mo": (now - 2592000,  "D"),
        "3mo": (now - 7776000,  "D"),
        "6mo": (now - 15552000, "W"),
        "1y":  (now - 31536000, "W"),
        "5y":  (now - 157680000,"M"),
    }
    frm, res = range_map.get(range, range_map["1mo"])
    try:
        data = await fh_get(f"/stock/candle?symbol={ticker}&resolution={res}&from={frm}&to={now}")
        if data.get("s") != "ok":
            return {"points": []}
        return {"points": [
            {"t": data["t"][i], "c": data["c"][i], "o": data["o"][i], "h": data["h"][i], "l": data["l"][i]}
            for i in range(len(data["t"]))
            if data["c"][i] is not None
        ]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/search")
async def search(q: str):
    try:
        data = await fh_get(f"/search?q={q}")
        results = [
            r for r in data.get("result", [])
            if r.get("type") == "Common Stock" and "." not in r.get("symbol", "")
        ][:10]
        return {"results": [{"symbol": r["symbol"], "name": r["description"]} for r in results]}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/indices")
async def get_indices():
    symbols = ["SPY", "QQQ", "DIA", "IWM", "GLD", "USO", "TLT"]
    labels  = {"SPY":"S&P 500","QQQ":"NASDAQ","DIA":"DOW","IWM":"RUSSELL","GLD":"GOLD","USO":"WTI OIL","TLT":"10Y BOND"}
    results = []
    for sym in symbols:
        try:
            q = await fh_get(f"/quote?symbol={sym}")
            results.append({
                "symbol": sym,
                "name":   labels.get(sym, sym),
                "price":  q.get("c"),
                "change": q.get("d"),
                "changePct": q.get("dp"),
            })
        except:
            results.append({"symbol": sym, "name": labels.get(sym, sym), "price": None, "changePct": 0})
    return {"indices": results}


@app.get("/api/movers")
async def get_movers(type: str = "gainers"):
    watchlist = ["AAPL","NVDA","MSFT","TSLA","AMZN","GOOGL","META","JPM","V","WMT",
                 "BAC","XOM","AMD","NFLX","PLTR","COIN","UBER","DIS","INTC","GS","CRM","ADBE"]
    results = []
    for sym in watchlist:
        try:
            q = await fh_get(f"/quote?symbol={sym}")
            if q.get("c") and q.get("dp") is not None:
                results.append({"symbol": sym, "price": q["c"], "changePct": q["dp"]})
        except:
            pass
    if type == "gainers":  results.sort(key=lambda x: x["changePct"], reverse=True)
    elif type == "losers": results.sort(key=lambda x: x["changePct"])
    else:                  results.sort(key=lambda x: abs(x["changePct"]), reverse=True)
    return {"movers": results[:8]}


@app.get("/api/intelligence/{ticker}")
async def get_intelligence(ticker: str):
    ticker = ticker.upper().strip()

    if not GEMINI_KEY:
        return {
            "sentiment": "Neutral",
            "score": 50,
            "verdict": "Configure GEMINI_API_KEY in Render environment variables to enable AI intelligence.",
            "risks": ["API key not configured"],
            "catalysts": ["Add GEMINI_API_KEY to enable full analysis"],
            "recommendation": "Hold"
        }

    try:
        from datetime import date, timedelta
        to_date   = date.today().isoformat()
        from_date = (date.today() - timedelta(days=5)).isoformat()

        quote_data   = await fh_get(f"/quote?symbol={ticker}")
        profile_data = await fh_get(f"/stock/profile2?symbol={ticker}")
        news_data    = await fh_get(f"/company-news?symbol={ticker}&from={from_date}&to={to_date}")

        price    = quote_data.get("c", "N/A")
        chg_pct  = quote_data.get("dp", 0)
        name     = profile_data.get("name", ticker)
        sector   = profile_data.get("finnhubIndustry", "Unknown")
        headlines = "\n".join([f"- {n['headline']}" for n in (news_data[:8] if isinstance(news_data, list) else [])])

        prompt = f"""You are an elite Wall Street analyst AI. Analyze {name} ({ticker}).

Current Price: ${price} | Change: {chg_pct:+.2f}% today
Sector: {sector}

Recent News Headlines:
{headlines or "No recent news available."}

Respond ONLY with a valid JSON object in this exact format:
{{
  "sentiment": "Bullish" or "Bearish" or "Neutral",
  "score": <integer 0-100, where 100=extremely bullish, 0=extremely bearish, 50=neutral>,
  "verdict": "<2-3 sentence sharp verdict on the stock right now>",
  "risks": ["<risk 1>", "<risk 2>", "<risk 3>"],
  "catalysts": ["<catalyst 1>", "<catalyst 2>"],
  "recommendation": "Strong Buy" or "Buy" or "Hold" or "Sell" or "Strong Sell"
}}

Be specific, data-driven, and direct. No fluff."""

        model    = genai.GenerativeModel("gemini-1.5-flash")
        response = model.generate_content(prompt)
        raw      = response.text.strip()

        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        parsed = json.loads(raw.strip())
        return parsed

    except json.JSONDecodeError:
        return {"sentiment":"Neutral","score":50,"verdict":"AI analysis temporarily unavailable — JSON parse error.","risks":[],"catalysts":[],"recommendation":"Hold"}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "finnhub": "configured" if FINNHUB_KEY else "missing",
        "gemini":  "configured" if GEMINI_KEY  else "missing",
    }


# ── serve frontend ────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def root():
    return FileResponse("static/index.html")

@app.get("/{full_path:path}")
async def catch_all(full_path: str):
    return FileResponse("static/index.html")
