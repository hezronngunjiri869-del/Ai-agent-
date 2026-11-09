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
from pypdf import PdfReader
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.common.exceptions import APIError
from http.server import BaseHTTPRequestHandler, HTTPServer
import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from telegram.request import HTTPXRequest
from tavily import TavilyClient
from upstash_redis import Redis

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
UPSTASH_URL = os.environ.get("UPSTASH_URL", "")
UPSTASH_TOKEN = os.environ.get("UPSTASH_TOKEN", "")
SHOTSTACK_KEY = os.environ.get("SHOTSTACK_KEY", "")
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")

cloudinary.config(cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME", ""), api_key=os.environ.get("CLOUDINARY_API_KEY", ""), api_secret=os.environ.get("CLOUDINARY_API_SECRET", ""))
tavily = TavilyClient(api_key=TAVILY_API_KEY)
redis = Redis(url=UPSTASH_URL, token=UPSTASH_TOKEN)
alpaca_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)

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
        return lessons if lessons else "No specific lessons learned yet."
    except:
        return "No lessons found."

def save_trading_lesson(new_rule):
    try:
        existing_lessons = get_trading_lessons()
        redis.set("trading_lessons", f"{existing_lessons}\n- {new_rule}")
    except:
        pass

def ask_groq(messages, max_tokens=1000):
    url = "https://api.groq.com/openai/v1/chat/completions"
    data = {"model": "openai/gpt-oss-20b", "messages": messages, "max_tokens": max_tokens}
    headers = {'Content-Type': 'application/json', 'Authorization': f'Bearer {GROQ_API_KEY}', 'User-Agent': 'HezzyAI/1.0'}
    req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
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
            return res['results'][0].get('raw_content', '')[:3000] 
    except Exception as e:
        return f"Extraction Error: {str(e)}"
    return "Could not extract text."

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
        return {"symbol": symbol.upper(), "price": round(usd_price, 2), "rsi": round(latest['RSI'], 2), "macd": "Bullish" if latest['MACD'] > latest['MACD_Signal'] else "Bearish"}
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
        return {"symbol": ticker.upper(), "price": round(latest['Close'], 2), "rsi": round(latest['RSI'], 2), "macd": "Bullish" if latest['MACD'] > latest['MACD_Signal'] else "Bearish"}
    except Exception as e:
        return {"error": f"Could not fetch stock data: {e}"}

def get_alpaca_balance():
    try:
        return float(alpaca_client.get_account().cash)
    except Exception:
        return 0.0

def get_alpaca_position(symbol):
    for attempt in range(3):
        try:
            pos = alpaca_client.get_open_position(symbol_or_asset_id=symbol)
            return float(pos.qty), float(pos.unrealized_plpc)
        except APIError as e:
            if e.status_code == 404: return 0.0, 0.0
            if e.status_code == 429:
                time.sleep(5)
                continue
            if "position does not exist" in str(e).lower(): return 0.0, 0.0
            time.sleep(3)
        except Exception:
            time.sleep(3)
    return None, None

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
        out = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        await edge_tts.Communicate(text, "en-US-BrianNeural").save(out.name)
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

def render_video(img_url, aud_url, text, duration=20.0):
    try:
        words = text.replace("\n", " ").split()
        chunks = [" ".join(words[i:i+3]).upper() for i in range(0, len(words), 3)]
        if not chunks: chunks = ["VIDEO"]
        time_chunk = duration / len(chunks)
        title_clips = [{"asset": {"type": "title", "text": c, "style": "subtitle", "size": "medium", "position": "bottom"}, "start": i * time_chunk, "length": time_chunk} for i, c in enumerate(chunks)]
        payload = {"timeline": {"background": "#000000", "tracks": [{"clips": [{"asset": {"type": "image", "src": img_url}, "start": 0, "length": duration}]}, {"clips": title_clips}], "soundtrack": {"src": aud_url, "effect": "fadeInFadeOut"}}, "output": {"format": "mp4", "resolution": "sd"}}
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

async def auto_trade_job(context: telegram.ext.CallbackContext):
    chat_id = redis.get("report_chat_id")
    if not chat_id: return
    assets = [("BTC", True), ("AAPL", False), ("ETH", True), ("TSLA", False)]
    report = "🤖 **Automated Trader Update**\n\n"
    lessons = get_trading_lessons()

    for asset, is_crypto in assets:
        await asyncio.sleep(2)
        if is_crypto:
            data = await asyncio.to_thread(get_crypto_data, asset)
            symbol = f"{asset}/USD"
            tif = TimeInForce.GTC
        else:
            data = await asyncio.to_thread(get_stock_data, asset)
            symbol = asset
            tif = TimeInForce.DAY
        
        if "error" in data or str(data.get('price')).lower() == 'nan':
            report += f"❌ {asset}: Analysis failed (Invalid Data).\n"
            continue
            
        position, pnl_pct = await asyncio.to_thread(get_alpaca_position, symbol)
        if position is None:
            report += f"⚠️ {asset}: API Timeout. Could not verify position.\n"
            continue

        if pnl_pct < -0.02:
            report += f"🛑 {asset}: Stop Loss Triggered! Position is down {pnl_pct*100:.2f}%. Forcing close.\n"
            try:
                qty = abs(position)
                side = OrderSide.BUY if position < 0 else OrderSide.SELL
                await asyncio.to_thread(alpaca_client.submit_order, order_data=MarketOrderRequest(symbol=symbol, qty=qty, side=side, time_in_force=tif))
                report += f"✅ {asset}: Closed position at ${data['price']}.\n"
            except Exception as e:
                report += f"❌ {asset}: Stop Loss failed. {str(e)[:50]}\n"
            continue

        prompt = f"Lessons: {lessons}\nAnalyze {asset}: Price: {data['price']}, RSI: {data['rsi']}, MACD: {data['macd']}. Reply ONLY ONE WORD: 'BUY' or 'SELL'."
        verdict = await asyncio.to_thread(ask_groq, [{"role": "user", "content": prompt}])
        verdict = verdict.strip().upper()
        
        if "BUY" in verdict:
            if position < 0:
                qty = abs(position)
                try:
                    await asyncio.to_thread(alpaca_client.submit_order, order_data=MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=tif))
                    report += f"✅ {asset}: Closed SHORT of {qty} at ${data['price']}.\n"
                except Exception as e:
                    report += f"❌ {asset}: Close Short failed. {str(e)[:50]}\n"
            elif position == 0:
                balance = await asyncio.to_thread(get_alpaca_balance)
                if balance < 100:
                    report += f"⚠️ {asset}: Not enough cash.\n"
                    continue
                qty = round(100 / data['price'], 4) if is_crypto else 1
                try:
                    await asyncio.to_thread(alpaca_client.submit_order, order_data=MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=tif))
                    report += f"✅ {asset}: Opened LONG of {qty} at ${data['price']}.\n"
                except Exception as e:
                    report += f"❌ {asset}: Open Long failed. {str(e)[:50]}\n"
            else:
                report += f"⏸️ {asset}: Already holding LONG. Skipped.\n"
        elif "SELL" in verdict:
            if position > 0:
                qty = position
                try:
                    await asyncio.to_thread(alpaca_client.submit_order, order_data=MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=tif))
                    report += f"✅ {asset}: Closed LONG of {qty} at ${data['price']}.\n"
                except Exception as e:
                    report += f"❌ {asset}: Close Long failed. {str(e)[:50]}\n"
            elif position == 0 and not is_crypto:
                try:
                    await asyncio.to_thread(alpaca_client.submit_order, order_data=MarketOrderRequest(symbol=symbol, qty=1, side=OrderSide.SELL, time_in_force=tif))
                    report += f"📉 {asset}: Opened SHORT of 1 at ${data['price']}.\n"
                except Exception as e:
                    report += f"❌ {asset}: Open Short failed. {str(e)[:50]}\n"
            elif position == 0 and is_crypto:
                report += f"⏸️ {asset}: Hold 0. Crypto cannot be shorted.\n"
            else:
                report += f"⏸️ {asset}: Already holding SHORT. Skipped.\n"
        else:
            report += f"⏸️ {asset}: AI verdict was HOLD.\n"

    try:
        await context.bot.send_message(chat_id=int(chat_id), text=report)
    except:
        pass

async def train_bot_daily(context: telegram.ext.CallbackContext):
    chat_id = redis.get("report_chat_id")
    if not chat_id: return
    prompt = "You are an elite trading coach. Write ONE short, strict rule to avoid losses."
    new_rule = await asyncio.to_thread(ask_groq, [{"role": "user", "content": prompt}])
    save_trading_lesson(new_rule)
    try:
        await context.bot.send_message(chat_id=int(chat_id), text=f"🧠 **Trader's Diary Updated**\n\n📝 *{new_rule}*")
    except:
        pass
async def start(update, context):
    await update.message.reply_text("Bot is alive. Commands:\n/analyze <asset>\n/video <topic>\n/trade <asset> <amount>\n/portfolio\n/task <prompt>\n/clear_pdf\n/report\n\nYou can also upload a PDF to ask questions!")

async def set_report(update, context):
    redis.set("report_chat_id", str(update.effective_chat.id))
    await update.message.reply_text("✅ Auto-Trader activated! I will analyze and trade every hour with a 2% Stop Loss.")

async def portfolio_cmd(update, context):
    await update.message.reply_text("💼 Fetching your portfolio...")
    try:
        account = await asyncio.to_thread(alpaca_client.get_account)
        positions = await asyncio.to_thread(alpaca_client.get_all_positions)
        msg = f"📊 **Portfolio Summary**\n\n💰 Cash Balance: ${float(account.cash):,.2f}\n📈 Total Equity: ${float(account.equity):,.2f}\n\n"
        if not positions: msg += "No open positions."
        else:
            msg += "**Open Positions:**\n"
            for p in positions:
                msg += f"- **{p.symbol}**: {p.qty} shares\n  Entry: ${float(p.avg_entry_price):,.2f} | Current: ${float(p.current_price):,.2f}\n  PnL: ${float(p.unrealized_pl):,.2f} ({float(p.unrealized_plpc)*100:.2f}%)\n\n"
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {str(e)}")

async def analyze_cmd(update, context):
    asset = (context.args[0] if context.args else "BTC").upper()
    await update.message.reply_text(f"📊 Analyzing {asset}...")
    if asset in ["BTC", "ETH", "SOL"]:
        data = await asyncio.to_thread(get_crypto_data, asset)
        news = await asyncio.to_thread(search_web, f"{asset} crypto news")
    else:
        data = await asyncio.to_thread(get_stock_data, asset)
        news = await asyncio.to_thread(search_web, f"{asset} stock news")
    if "error" in data: return await update.message.reply_text(f"❌ {data['error']}")
    lessons = get_trading_lessons()
    prompt = f"Lessons: {lessons}\nAnalyze {asset}:\nPrice: {data['price']}\nRSI: {data['rsi']}\nMACD: {data['macd']}\nNews: {news}\nProvide: 1. Verdict (BUY/SELL/HOLD). 2. Confidence. 3. Reasoning."
    await update.message.reply_text(await asyncio.to_thread(ask_groq, [{"role": "user", "content": prompt}]))

async def task_cmd(update, context):
    task_prompt = " ".join(context.args)
    if not task_prompt:
        return await update.message.reply_text("Usage: /task <paste the prompt here>")
    await update.message.reply_text("🧠 Analyzing task and generating expert response...")
    system_prompt = "You are an elite AI data annotator. Complete the following task with perfect accuracy. Provide ONLY the final output requested without conversational filler."
    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": task_prompt}]
    reply = await asyncio.to_thread(ask_groq, messages, 2000)
    await update.message.reply_text(f"✅ **Task Complete:**\n\n{reply}")

async def trade_cmd(update, context):
    args = context.args
    if len(args) < 2: return await update.message.reply_text("Usage: /trade <asset> <amount>")
    asset = args[0].upper()
    try: quantity = float(args[1])
    except: return await update.message.reply_text("Invalid amount.")
    await update.message.reply_text(f"📊 Analyzing {asset}...")
    is_crypto = asset in ["BTC", "ETH", "SOL"]
    if is_crypto:
        data = await asyncio.to_thread(get_crypto_data, asset)
        symbol = f"{asset}/USD"
        tif = TimeInForce.GTC
    else:
        data = await asyncio.to_thread(get_stock_data, asset)
        symbol = asset
        tif = TimeInForce.DAY
    if "error" in data: 
        return await update.message.reply_text(f"❌ {data['error']}")
    lessons = get_trading_lessons()
    prompt = f"Lessons: {lessons}\nAnalyze {asset}: Price: {data['price']}, RSI: {data['rsi']}, MACD: {data['macd']}. Reply ONLY ONE WORD: 'BUY' or 'SELL'."
    verdict = await asyncio.to_thread(ask_groq, [{"role": "user", "content": prompt}])
    verdict = verdict.strip().upper()
    if "BUY" in verdict: side = OrderSide.BUY
    elif "SELL" in verdict: side = OrderSide.SELL
    else: return await update.message.reply_text(f"🤖 AI said {verdict}.")
    if side == OrderSide.SELL and is_crypto:
        position, _ = await asyncio.to_thread(get_alpaca_position, symbol)
        if position is None or position < quantity: 
            return await update.message.reply_text(f"⚠️ Blocked: Hold {position} {asset}.")
    if side == OrderSide.BUY:
        balance = await asyncio.to_thread(get_alpaca_balance)
        cost = data['price'] * quantity
        if balance < cost: 
            return await update.message.reply_text(f"⚠️ Blocked: Need ${cost:,.2f}.")
    try:
        await asyncio.to_thread(alpaca_client.submit_order, order_data=MarketOrderRequest(symbol=symbol, qty=quantity, side=side, time_in_force=tif))
        await update.message.reply_text(f"✅ Executed: {side} {quantity} {symbol}.")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {str(e)}")

async def video_cmd(update, context):
    topic = " ".join(context.args)
    if not topic: return await update.message.reply_text("Example: /video AI in Kenya")
    await update.message.reply_text(f"🎬 Building video for: {topic}...")
    chat_id = update.effective_chat.id
    async def process():
        try:
            script = await asyncio.to_thread(ask_groq, [{"role": "user", "content": f"Write a 15-second TikTok script (35-40 words) about {topic}."}])
            img_bytes = await asyncio.to_thread(make_image, f"Cinematic background for {topic}")
            aud_bytes = await make_audio(script)
            if not img_bytes or not aud_bytes: return await context.bot.send_message(chat_id, "Failed to make media.")
            img_url = await asyncio.to_thread(upload_cloud, img_bytes, "img.jpg")
            aud_url = await asyncio.to_thread(upload_cloud, aud_bytes, "aud.mp3")
            await context.bot.send_message(chat_id, f"✅ Video Ready:\n{await asyncio.to_thread(render_video, img_url, aud_url, script)}")
        except Exception as e:
            await context.bot.send_message(chat_id, f"Error: {e}")
    asyncio.create_task(process())

# --- PDF READER FEATURE ---
async def handle_pdf(update, context):
    user_id = str(update.effective_user.id)
    await update.message.reply_text("📄 Downloading and reading PDF... This may take a moment.")
    
    try:
        file = await update.message.document.get_file()
        temp_path = f"doc_{user_id}.pdf"
        await file.download_to_drive(temp_path)
        
        reader = PdfReader(temp_path)
        text = ""
        for page in reader.pages:
            text += page.extract_text() + "\n"
            if len(text) > 15000: # Limit to avoid Groq token limits
                break
                
        redis.set(f"pdf_ctx:{user_id}", text)
        await update.message.reply_text(f"✅ PDF loaded! ({len(reader.pages)} pages read).\n\nI am now ready to answer questions based on this document. Just ask!\n(Use /clear_pdf to remove it from memory).")
    except Exception as e:
        await update.message.reply_text(f"❌ Error reading PDF: {str(e)}")
    finally:
        if os.path.exists(temp_path): os.remove(temp_path)

async def clear_pdf_cmd(update, context):
    user_id = str(update.effective_user.id)
    redis.delete(f"pdf_ctx:{user_id}")
    await update.message.reply_text("🧹 PDF context cleared from memory.")

async def handle_voice(update, context):
    await update.message.reply_text("🎙️ Transcribing...")
    voice_file = await update.message.voice.get_file()
    temp_path = f"voice_{update.effective_user.id}.ogg"
    await voice_file.download_to_drive(temp_path)
    transcribed_text = await asyncio.to_thread(transcribe_voice, temp_path)
    if not transcribed_text or "Error" in transcribed_text:
        return await update.message.reply_text(f"❌ Could not understand: {transcribed_text}")
    await update.message.reply_text(f"🗣️ You said: \"{transcribed_text}\"")
    update.message.text = transcribed_text
    await handle_msg(update, context) 
    if os.path.exists(temp_path): os.remove(temp_path)

async def handle_msg(update, context):
    user_id = str(update.effective_user.id)
    text = update.message.text
    lower_text = text.lower()
    history = get_memory(user_id)
    
    if "portfolio" in lower_text or "balance" in lower_text or "positions" in lower_text:
        return await portfolio_cmd(update, context)
    if "analyze" in lower_text and len(lower_text.split()) > 1:
        context.args = lower_text.split()[1:]
        return await analyze_cmd(update, context)
    if "task" in lower_text and len(lower_text.split()) > 1:
        context.args = lower_text.split()[1:]
        return await task_cmd(update, context)
        
    url_match = re.search(r'https?://\S+', text)
    if url_match:
        await update.message.reply_text("🔗 Reading link...")
        content = await asyncio.to_thread(read_link, url_match.group(0))
        if "Error" in content or "Could not" in content: return await update.message.reply_text(f"❌ {content}")
        await update.message.reply_text(await asyncio.to_thread(ask_groq, [{"role": "user", "content": f"Content: {content}\nProvide summary."}]))
        return
        
    pdf_context = redis.get(f"pdf_ctx:{user_id}")
    if pdf_context:
        system_prompt = f"You are Hezzy's assistant. Use the following PDF content to answer the user's question accurately:\n\n--- PDF START ---\n{pdf_context}\n--- PDF END ---"
    else:
        system_prompt = "You are Hezzy's assistant."
        
    history.append({"role": "user", "content": text})
    reply = await asyncio.to_thread(ask_groq, [{"role": "system", "content": system_prompt}] + history)
    history.append({"role": "assistant", "content": reply})
    save_memory(user_id, history)
    await update.message.reply_text(reply)

def main():
    start_dummy_server()
    req = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    app = Application.builder().token(TELEGRAM_TOKEN).request(req).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("report", set_report))
    app.add_handler(CommandHandler("portfolio", portfolio_cmd))
    app.add_handler(CommandHandler("analyze", analyze_cmd))
    app.add_handler(CommandHandler("trade", trade_cmd))
    app.add_handler(CommandHandler("video", video_cmd))
    app.add_handler(CommandHandler("task", task_cmd))
    app.add_handler(CommandHandler("clear_pdf", clear_pdf_cmd))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))
    
    job_queue = app.job_queue
    if job_queue:
        job_queue.run_repeating(auto_trade_job, interval=3600, first=10)
        job_queue.run_repeating(train_bot_daily, interval=86400, first=3600)
        
    while True:
        try:
            print("Bot is running with PDF Reader...")
            app.run_polling(drop_pending_updates=True)
        except Exception as e:
            print(f"Restarting due to {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
