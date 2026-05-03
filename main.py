import os
import time
import json
import asyncio
import httpx
from datetime import date, timedelta
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "")
GEMINI_KEY  = os.environ.get("GEMINI_API_KEY", "")

gemini_model = None
if GEMINI_KEY:
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_KEY)
        gemini_model = genai.GenerativeModel("gemini-1.5-flash")
        print("Gemini configured OK")
    except Exception as e:
        print(f"Gemini init failed: {e}")

app = FastAPI(title="NEXUS API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

FH = "https://api.finnhub.io/api/v1"

async def fh(path: str):
    if not FINNHUB_KEY:
        raise HTTPException(503, "FINNHUB_API_KEY not configured in Render environment")
    sep = "&" if "?" in path else "?"
    url = f"{FH}{path}{sep}token={FINNHUB_KEY}"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url)
        if r.status_code == 429:
            await asyncio.sleep(1)
            r = await client.get(url)
        if r.status_code == 403:
            raise HTTPException(403, f"Finnhub 403 — check your API key is valid")
        if r.status_code != 200:
            raise HTTPException(r.status_code, f"Finnhub error {r.status_code}")
        return r.json()

def fmt_big(v):
    if not v: return "—"
    v = float(v)
    if v >= 1e12: return f"${v/1e12:.2f}T"
    if v >= 1e9:  return f"${v/1e9:.2f}B"
    if v >= 1e6:  return f"${v/1e6:.2f}M"
    return f"${v:,.0f}"

@app.get("/api/health")
async def health():
    status = {"status": "ok", "finnhub": "configured" if FINNHUB_KEY else "MISSING", "gemini": "configured" if GEMINI_KEY else "not set"}
    if FINNHUB_KEY:
        try:
            q = await fh("/quote?symbol=AAPL")
            status["test_price"] = q.get("c", "no data")
        except Exception as e:
            status["test_error"] = str(e)
    return status

@app.get("/api/quote/{ticker}")
async def quote(ticker: str):
    ticker = ticker.upper().strip()
    try:
        q = await fh(f"/quote?symbol={ticker}")
        await asyncio.sleep(0.15)
        p = await fh(f"/stock/profile2?symbol={ticker}")

        price  = q.get("c") or 0
        prev   = q.get("pc") or price
        change = round(price - prev, 2)
        pct    = round((change / prev) * 100, 2) if prev else 0
        mc_raw = (p.get("marketCapitalization") or 0) * 1e6

        pe = eps = beta = div_yield = None
        try:
            await asyncio.sleep(0.15)
            m   = await fh(f"/stock/metric?symbol={ticker}&metric=all")
            met = m.get("metric", {})
            pe        = met.get("peNormalizedAnnual")
            eps       = met.get("epsNormalizedAnnual")
            beta      = met.get("beta")
            div_yield = met.get("dividendYieldIndicatedAnnual")
            w52h      = met.get("52WeekHigh")
            w52l      = met.get("52WeekLow")
            avol      = met.get("averageDailyVolume10Day")
        except:
            w52h = w52l = avol = None

        return {
            "symbol":        ticker,
            "name":          p.get("name", ticker),
            "sector":        p.get("finnhubIndustry", "—"),
            "logo":          p.get("logo", ""),
            "price":         round(price, 2),
            "change":        change,
            "changePct":     pct,
            "open":          q.get("o"),
            "high":          q.get("h"),
            "low":           q.get("l"),
            "prevClose":     q.get("pc"),
            "marketCap":     fmt_big(mc_raw),
            "peRatio":       round(pe, 2) if pe else None,
            "eps":           round(eps, 2) if eps else None,
            "beta":          round(beta, 2) if beta else None,
            "dividendYield": round(div_yield, 2) if div_yield else None,
            "week52High":    round(w52h, 2) if w52h else None,
            "week52Low":     round(w52l, 2) if w52l else None,
            "avgVolume":     fmt_big(avol) if avol else "—",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/api/candles/{ticker}")
async def candles(ticker: str, range: str = "1mo"):
    ticker = ticker.upper().strip()
    now = int(time.time())
    cfg = {
        "1d":  (now - 86400,     "15"),
        "5d":  (now - 432000,    "60"),
        "1mo": (now - 2592000,   "D"),
        "3mo": (now - 7776000,   "D"),
        "6mo": (now - 15552000,  "W"),
        "1y":  (now - 31536000,  "W"),
        "5y":  (now - 157680000, "M"),
    }
    frm, res = cfg.get(range, cfg["1mo"])
    try:
        d = await fh(f"/stock/candle?symbol={ticker}&resolution={res}&from={frm}&to={now}")
        if d.get("s") != "ok" or not d.get("t"):
            return {"points": []}
        return {"points": [{"t": d["t"][i], "c": d["c"][i]} for i in range(len(d["t"])) if d["c"][i] is not None]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/api/news/{ticker}")
async def news(ticker: str):
    ticker = ticker.upper().strip()
    to_d = date.today().isoformat()
    fr_d = (date.today() - timedelta(days=7)).isoformat()
    try:
        d = await fh(f"/company-news?symbol={ticker}&from={fr_d}&to={to_d}")
        if not isinstance(d, list):
            return {"news": []}
        return {"news": [{"headline": n.get("headline",""), "source": n.get("source",""), "url": n.get("url","#"), "datetime": n.get("datetime",0)} for n in d[:15]]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/api/search")
async def search(q: str = ""):
    try:
        d = await fh(f"/search?q={q}")
        results = [r for r in d.get("result", []) if r.get("type") == "Common Stock" and "." not in r.get("symbol","")][:10]
        return {"results": [{"symbol": r["symbol"], "name": r["description"]} for r in results]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/api/indices")
async def indices():
    items = [("SPY","S&P 500"),("QQQ","NASDAQ"),("DIA","DOW"),("IWM","RUSSELL"),("GLD","GOLD"),("USO","WTI OIL"),("TLT","10Y BOND")]
    results = []
    for sym, label in items:
        try:
            await asyncio.sleep(0.1)
            q = await fh(f"/quote?symbol={sym}")
            price = q.get("c") or 0
            prev  = q.get("pc") or price
            pct   = round(((price-prev)/prev)*100, 2) if prev else 0
            results.append({"symbol": sym, "name": label, "price": round(price,2), "changePct": pct})
        except:
            results.append({"symbol": sym, "name": label, "price": None, "changePct": 0})
    return {"indices": results}

@app.get("/api/movers")
async def movers(type: str = "gainers"):
    watchlist = ["AAPL","NVDA","MSFT","TSLA","AMZN","GOOGL","META","JPM","V","WMT","BAC","XOM","AMD","NFLX","PLTR","GS"]
    results = []
    for sym in watchlist:
        try:
            await asyncio.sleep(0.1)
            q = await fh(f"/quote?symbol={sym}")
            price = q.get("c") or 0
            prev  = q.get("pc") or price
            pct   = round(((price-prev)/prev)*100, 2) if prev else 0
            results.append({"symbol": sym, "price": round(price,2), "changePct": pct})
        except:
            pass
    if type == "gainers":  results.sort(key=lambda x: x["changePct"], reverse=True)
    elif type == "losers": results.sort(key=lambda x: x["changePct"])
    else:                  results.sort(key=lambda x: abs(x["changePct"]), reverse=True)
    return {"movers": results[:8]}

@app.get("/api/intelligence/{ticker}")
async def intelligence(ticker: str):
    ticker = ticker.upper().strip()
    if not gemini_model:
        return {"sentiment":"Neutral","score":50,"verdict":"Add GEMINI_API_KEY in Render environment to enable AI analysis.","risks":["GEMINI_API_KEY not configured"],"catalysts":[],"recommendation":"Hold"}
    try:
        q_data = await fh(f"/quote?symbol={ticker}")
        await asyncio.sleep(0.15)
        p_data = await fh(f"/stock/profile2?symbol={ticker}")
        to_d = date.today().isoformat()
        fr_d = (date.today() - timedelta(days=5)).isoformat()
        await asyncio.sleep(0.15)
        n_data = await fh(f"/company-news?symbol={ticker}&from={fr_d}&to={to_d}")

        price = q_data.get("c", 0)
        prev  = q_data.get("pc", price)
        pct   = round(((price-prev)/prev)*100,2) if prev else 0
        name  = p_data.get("name", ticker)
        sector= p_data.get("finnhubIndustry","Unknown")
        headlines = "\n".join([f"- {n['headline']}" for n in (n_data[:6] if isinstance(n_data,list) else [])]) or "No recent headlines."

        prompt = f"""You are an elite Wall Street analyst. Analyze {name} ({ticker}).
Price: ${price} | Change: {pct:+.2f}% | Sector: {sector}
Headlines:
{headlines}

Reply ONLY with valid JSON (no markdown, no backticks):
{{"sentiment":"Bullish or Bearish or Neutral","score":0-100,"verdict":"2-3 sentences","risks":["risk1","risk2","risk3"],"catalysts":["cat1","cat2"],"recommendation":"Strong Buy or Buy or Hold or Sell or Strong Sell"}}"""

        resp = gemini_model.generate_content(prompt)
        raw  = resp.text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"sentiment":"Neutral","score":50,"verdict":"AI parse error.","risks":[],"catalysts":[],"recommendation":"Hold"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def root():
    return FileResponse("static/index.html")

@app.get("/{full_path:path}")
async def spa(full_path: str):
    return FileResponse("static/index.html")
