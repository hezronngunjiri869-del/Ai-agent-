import os
import threading
import tempfile
import asyncio
import time
import re
import json
import urllib.request
import urllib.parse
import urllib.error
import edge_tts
import cloudinary
import cloudinary.uploader
import yfinance as yf
import pandas as pd
import requests
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from http.server import BaseHTTPRequestHandler, HTTPServer
import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from telegram.request import HTTPXRequest
from tavily import TavilyClient
from upstash_redis import Redis

# --- KEYS ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
UPSTASH_URL = os.environ.get("UPSTASH_URL", "")
UPSTASH_TOKEN = os.environ.get("UPSTASH_TOKEN", "")
SHOTSTACK_KEY = os.environ.get("SHOTSTACK_KEY", "")
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")

cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME", ""),
    api_key=os.environ.get("CLOUDINARY_API_KEY", ""),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET", "")
)

tavily = TavilyClient(api_key=TAVILY_API_KEY)
redis = Redis(url=UPSTASH_URL, token=UPSTASH_TOKEN)

# --- ALPACA CLIENT (PAPER TRADING) ---
alpaca_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)

# --- WEB SERVER ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def start_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

# --- MEMORY & SELF-TRAINING ---
def get_memory(user_id):
    try:
        data = redis.get(f"mem:{user_id}")
        return json.loads(data) if data else []
    except:
        return []

def save_memory(user_id, history):
    try:
        redis.set(f"mem:{user_id}", json.dumps(history[-10:]))
    except:
        pass

def get_trading_lessons():
    try:
        lessons = redis.get("trading_lessons")
        return lessons if lessons else "No specific lessons learned yet. Stick to basic RSI and MACD."
    except:
        return "No lessons found."

def save_trading_lesson(new_rule):
    try:
        existing_lessons = get_trading_lessons()
        # Keep the last 5 lessons to avoid token limits
        updated_lessons = f"{existing_lessons}\n- {new_rule}"
        redis.set("trading_lessons", updated_lessons)
    except:
        pass

# --- AI ---
def ask_groq(messages):
    url = "https://api.groq.com/openai/v1/chat/completions"
    data = {"model": "openai/gpt-oss-20b", "messages": messages, "max_tokens": 1000}
    headers = {
        'Content-Type': 'application/json', 
        'Authorization': f'Bearer {GROQ_API_KEY}',
        'User-Agent': 'HezzyAI/1.0'
    }
    req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode('utf-8'))['choices'][0]['message']['content']
    except Exception as e:
        return f"AI Error: {str(e)}"

def transcribe_voice(file_path):
    try:
        url = "https://api.groq.com/openai/v1/audio/transcriptions"
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
        files = {"file": ("voice.ogg", open(file_path, "rb"))}
        data = {"model": "whisper-large-v3"}
        
        response = requests.post(url, headers=headers, files=files, data=data, timeout=30)
        return response.json().get("text", "")
    except Exception as e:
        return f"Voice Error: {e}"

# --- SEARCH & LINK ---
def search_web(query):
    try:
        res = tavily.search(query=query, max_results=3)
        return "\n".join([f"- {r['title']}: {r['content'][:200]}" for r in res['results']])
    except:
        return "Search failed."

def read_link(url):
    if "tiktok.com" in url:
        try:
            req = urllib.request.Request(f"https://www.tiktok.com/oembed?url={url}", headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as res:
                data = json.loads(res.read().decode('utf-8'))
                return f"TikTok by @{data.get('author_name')}\nCaption: {data.get('title')}"
        except:
            return "Failed to read TikTok link."
    try:
        res = tavily.extract(urls=[url])
        if res and 'results' in res and len(res['results']) > 0:
            content = res['results'][0].get('raw_content', '')
            if not content:
                return "Could not extract text from this site."
            return content[:3000] 
    except Exception as e:
        return f"Extraction Error: {str(e)}"
    return "Could not extract text from this site."

# --- TECHNICAL INDICATORS ---
def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calculate_macd(series, fast=12, slow=26, signal=9):
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line

# --- MARKET DATA ---
def get_crypto_data(symbol="BTC"):
    try:
        coin_map = {"BTC": "btc-bitcoin", "ETH": "eth-ethereum", "SOL": "sol-solana"}
        coin_id = coin_map.get(symbol.upper(), "btc-bitcoin")
        
        start_time = time.strftime('%Y-%m-%d', time.gmtime(time.time() - 86400*100))
        url = f"https://api.coinpaprika.com/v1/tickers/{coin_id}/historical?start={start_time}&interval=1d"
        req = urllib.request.Request(url, headers={'User-Agent': 'HezzyAI/1.0'})
        with urllib.request.urlopen(req, timeout=20) as res:
            data = json.loads(res.read().decode('utf-8'))
        
        df = pd.DataFrame(data)
        df['close'] = df['price'].astype(float)
        df['RSI'] = calculate_rsi(df['close'], 14)
        macd_line, signal_line = calculate_macd(df['close'])
        df['MACD'] = macd_line
        df['MACD_Signal'] = signal_line
        latest = df.iloc[-1]
        
        price_url = f"https://api.coinpaprika.com/v1/tickers/{coin_id}"
        req_price = urllib.request.Request(price_url, headers={'User-Agent': 'HezzyAI/1.0'})
        with urllib.request.urlopen(req_price, timeout=15) as res:
            price_data = json.loads(res.read().decode('utf-8'))
            usd_price = float(price_data['quotes']['USD']['price'])
            
        return {
            "symbol": symbol.upper(),
            "price": round(usd_price, 2),
            "rsi": round(latest['RSI'], 2),
            "macd": "Bullish" if latest['MACD'] > latest['MACD_Signal'] else "Bearish"
        }
    except Exception as e:
        return {"error": f"Could not fetch crypto data: {e}"}

def get_stock_data(ticker="AAPL"):
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="60d")
        if hist.empty: return {"error": f"No data for {ticker}."}
        
        hist['RSI'] = calculate_rsi(hist['Close'], 14)
        macd_line, signal_line = calculate_macd(hist['Close'])
        hist['MACD'] = macd_line
        hist['MACD_Signal'] = signal_line
        
        latest = hist.iloc[-1]
        return {
            "symbol": ticker.upper(),
            "price": round(latest['Close'], 2),
            "rsi": round(latest['RSI'], 2),
            "macd": "Bullish" if latest['MACD'] > latest['MACD_Signal'] else "Bearish"
        }
    except Exception as e:
        return {"error": f"Could not fetch stock data: {e}"}

# --- ACCOUNT CHECKERS ---
def get_alpaca_balance():
    try:
        account = alpaca_client.get_account()
        return float(account.cash)
    except Exception:
        return 0.0

def get_alpaca_position(symbol):
    try:
        position = alpaca_client.get_open_position(symbol_or_asset_id=symbol)
        return float(position.qty)
    except Exception:
        return 0.0

# --- MEDIA GENERATION ---
def make_image(prompt):
    try:
        url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(prompt)}?width=1024&height=1024&nologo=true"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.read()
    except:
        return None

async def make_audio(text):
    try:
        voice = "en-US-BrianNeural"
        out = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        await edge_tts.Communicate(text, voice).save(out.name)
        with open(out.name, "rb") as f:
            data = f.read()
        os.unlink(out.name)
        return data
    except:
        return None

def upload_cloud(file_bytes, name):
    try:
        temp = tempfile.NamedTemporaryFile(delete=False, suffix=f"_{name}")
        temp.write(file_bytes)
        temp.close()
        res = cloudinary.uploader.upload(temp.name, resource_type="auto")
        os.unlink(temp.name)
        return res['secure_url']
    except:
        return None

# --- SHOTSTACK VIDEO ---
def render_video(img_url, aud_url, text, duration=20.0):
    try:
        words = text.replace("\n", " ").split()
        chunks = [" ".join(words[i:i+3]).upper() for i in range(0, len(words), 3)]
        if not chunks: chunks = ["VIDEO"]
        time_chunk = duration / len(chunks)
        
        title_clips = []
        for i, chunk in enumerate(chunks):
            title_clips.append({
                "asset": {"type": "title", "text": chunk, "style": "subtitle", "size": "medium", "position": "bottom"},
                "start": i * time_chunk, "length": time_chunk
            })

        payload = {
            "timeline": {
                "background": "#000000",
                "tracks": [
                    {"clips": [{"asset": {"type": "image", "src": img_url}, "start": 0, "length": duration}]},
                    {"clips": title_clips}
                ],
                "soundtrack": {"src": aud_url, "effect": "fadeInFadeOut"}
            },
            "output": {"format": "mp4", "resolution": "sd"}
        }
        
        req = urllib.request.Request("https://api.shotstack.io/stage/render", data=json.dumps(payload).encode('utf-8'), headers={'x-api-key': SHOTSTACK_KEY, 'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=30) as res:
            render_id = json.loads(res.read().decode('utf-8'))['response']['id']
            
        for _ in range(30):
            time.sleep(3)
            req_status = urllib.request.Request(f"https://api.shotstack.io/stage/render/{render_id}", headers={'x-api-key': SHOTSTACK_KEY})
            with urllib.request.urlopen(req_status, timeout=15) as res:
                data = json.loads(res.read().decode('utf-8'))['response']
                if data['status'] == 'done': return data['url']
                if data['status'] == 'failed': return f"Error: {data.get('error')}"
        return "Render timed out."
    except Exception as e:
        return f"Render failed: {str(e)}"

# --- AUTOMATED TRADING JOB (WITH SELF-TRAINING) ---
                except Exception as e:
                    report += f"❌ {asset}: Short failed. {str(e)[:50]}\n"
            else:
                report += f"⏸️ {asset}: AI said SELL, but you hold 0. Crypto cannot be shorted.\n"
        else:
            report += f"⏸️ {asset}: AI verdict was HOLD. No action.\n"

    try:
        await context.bot.send_message(chat_id=int(chat_id), text=report)
    except:
        pass

# --- DAILY REFLECTION JOB (THE TRAINING) ---
 # --- AUTOMATED TRADING JOB (WITH POSITION CHECK & NAN FIX) ---
async def auto_trade_job(context: telegram.ext.CallbackContext):
    chat_id = redis.get("report_chat_id")
    if not chat_id: return

    assets = [("BTC", True), ("AAPL", False), ("ETH", True), ("TSLA", False)]
    report = "🤖 **Automated Trader Update**\n\n"
    
    lessons = get_trading_lessons()

    for asset, is_crypto in assets:
        if is_crypto:
            data = await asyncio.to_thread(get_crypto_data, asset)
            symbol = f"{asset}/USD"
            tif = TimeInForce.GTC
        else:
            data = await asyncio.to_thread(get_stock_data, asset)
            symbol = asset
            tif = TimeInForce.DAY

        # Check for errors or NaN data
        if "error" in data or str(data.get('price')).lower() == 'nan':
            report += f"❌ {asset}: Analysis failed (Invalid Data).\n"
            continue

        prompt = f"""You are an expert trader. 
        Here are the lessons you learned from past mistakes:
        {lessons}
        
        Analyze this data for {asset}: Price: {data['price']}, RSI: {data['rsi']}, MACD: {data['macd']}. 
        Decide whether to BUY or SELL. Reply with ONLY ONE WORD: 'BUY' or 'SELL'."""
        
        verdict = await asyncio.to_thread(ask_groq, [{"role": "user", "content": prompt}])
        verdict = verdict.strip().upper()

        # Get current position
        position = await asyncio.to_thread(get_alpaca_position, symbol)
        
        if "BUY" in verdict:
            if position < 0:
                # We are SHORT. We must BUY to close it.
                qty = abs(position)
                try:
                    order_data = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=tif)
                    await asyncio.to_thread(alpaca_client.submit_order, order_data=order_data)
                    report += f"✅ {asset}: AI said BUY. Closed SHORT of {qty} at ${data['price']}.\n"
                except Exception as e:
                    report += f"❌ {asset}: Close Short failed. {str(e)[:50]}\n"
            elif position == 0:
                # We are FLAT. We open a LONG.
                balance = await asyncio.to_thread(get_alpaca_balance)
                if balance < 100:
                    report += f"⚠️ {asset}: Not enough cash to buy.\n"
                    continue
                qty = round(100 / data['price'], 4) if is_crypto else 1
                try:
                    order_data = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=tif)
                    await asyncio.to_thread(alpaca_client.submit_order, order_data=order_data)
                    report += f"✅ {asset}: AI said BUY. Opened LONG of {qty} at ${data['price']}.\n"
                except Exception as e:
                    report += f"❌ {asset}: Open Long failed. {str(e)[:50]}\n"
            else:
                # We are already LONG. Do nothing.
                report += f"⏸️ {asset}: AI said BUY, but already holding LONG. Skipped.\n"

        elif "SELL" in verdict:
            if position > 0:
                # We are LONG. We must SELL to close it.
                qty = position
                try:
                    order_data = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=tif)
                    await asyncio.to_thread(alpaca_client.submit_order, order_data=order_data)
                    report += f"✅ {asset}: AI said SELL. Closed LONG of {qty} at ${data['price']}.\n"
                except Exception as e:
                    report += f"❌ {asset}: Close Long failed. {str(e)[:50]}\n"
            elif position == 0 and not is_crypto:
                # We are FLAT and it's a stock. Open a SHORT.
                try:
                    order_data = MarketOrderRequest(symbol=symbol, qty=1, side=OrderSide.SELL, time_in_force=tif)
                    await asyncio.to_thread(alpaca_client.submit_order, order_data=order_data)
                    report += f"📉 {asset}: AI said SELL. Opened SHORT of 1 at ${data['price']}.\n"
                except Exception as e:
                    report += f"❌ {asset}: Open Short failed. {str(e)[:50]}\n"
            elif position == 0 and is_crypto:
                report += f"⏸️ {asset}: AI said SELL, but you hold 0. Crypto cannot be shorted.\n"
            else:
                # We are already SHORT. Do nothing.
                report += f"⏸️ {asset}: AI said SELL, but already holding SHORT. Skipped.\n"
        else:
            report += f"⏸️ {asset}: AI verdict was HOLD. No action.\n"

    try:
        await context.bot.send_message(chat_id=int(chat_id), text=report)
    except:
        pass
