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

# --- MEMORY ---
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

# --- AUTOMATED TRADING JOB ---
async def auto_trade_job(context: telegram.ext.CallbackContext):
    chat_id = redis.get("report_chat_id")
    if not chat_id:
        return # User hasn't set up reports/notifications yet

    assets = [("BTC", True), ("AAPL", False)] # (Asset, is_crypto)
    report = "🤖 **Automated Trader Update**\n\n"

    for asset, is_crypto in assets:
        if is_crypto:
            data = await asyncio.to_thread(get_crypto_data, asset)
            symbol = f"{asset}/USD"
            tif = TimeInForce.GTC
        else:
            data = await asyncio.to_thread(get_stock_data, asset)
            symbol = asset
            tif = TimeInForce.DAY

        if "error" in data:
            report += f"❌ {asset}: Analysis failed.\n"
            continue

        prompt = f"You are an expert trader. Analyze this data for {asset}: Price: {data['price']}, RSI: {data['rsi']}, MACD: {data['macd']}. Decide whether to BUY or SELL. Reply with ONLY ONE WORD: 'BUY' or 'SELL'."
        verdict = await asyncio.to_thread(ask_groq, [{"role": "user", "content": prompt}])
        verdict = verdict.strip().upper()

        if "BUY" in verdict:
            balance = await asyncio.to_thread(get_alpaca_balance)
            if balance < 100:
                report += f"⚠️ {asset}: Not enough cash to buy.\n"
                continue
                
            if is_crypto:
                qty = round(100 / data['price'], 4)
            else:
                qty = 1

            try:
                order_data = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=tif)
                await asyncio.to_thread(alpaca_client.submit_order, order_data=order_data)
                report += f"✅ {asset}: AI said BUY. Bought {qty} at ${data['price']}.\n"
            except Exception as e:
                report += f"❌ {asset}: Buy failed. {str(e)[:50]}\n"

        elif "SELL" in verdict:
            position = await asyncio.to_thread(get_alpaca_position, symbol)
            if position > 0:
                try:
                    order_data = MarketOrderRequest(symbol=symbol, qty=position, side=OrderSide.SELL, time_in_force=tif)
                    await asyncio.to_thread(alpaca_client.submit_order, order_data=order_data)
                    report += f"✅ {asset}: AI said SELL. Sold {position} at ${data['price']}.\n"
                except Exception as e:
                    report += f"❌ {asset}: Sell failed. {str(e)[:50]}\n"
            else:
                report += f"⏸️ {asset}: AI said SELL, but you hold 0. Skipped.\n"
        else:
            report += f"⏸️ {asset}: AI verdict was HOLD/Unclear. No action.\n"

    try:
        await context.bot.send_message(chat_id=int(chat_id), text=report)
    except:
        pass

# --- TELEGRAM HANDLERS ---
async def start(update, context):
    await update.message.reply_text("Bot is alive. Commands:\n/analyze <asset>\n/video <topic>\n/trade <asset> <amount>\n/portfolio\n/report (Enable auto-trading & reports)")

async def set_report(update, context):
    chat_id = str(update.effective_chat.id)
    redis.set("report_chat_id", chat_id)
    await update.message.reply_text("✅ Auto-Trader activated! I will analyze and trade BTC & AAPL every 5 minutes, and send you a report.")

async def portfolio_cmd(update, context):
    await update.message.reply_text("💼 Fetching your portfolio...")
    try:
        account = await asyncio.to_thread(alpaca_client.get_account)
        cash = float(account.cash)
        equity = float(account.equity)
        
        positions = await asyncio.to_thread(alpaca_client.get_all_positions)
        
        msg = f"📊 **Portfolio Summary**\n\n"
        msg += f"💰 Cash Balance: ${cash:,.2f}\n"
        msg += f"📈 Total Equity: ${equity:,.2f}\n\n"
        
        if not positions:
            msg += "No open positions."
        else:
            msg += "**Open Positions:**\n"
            for p in positions:
                msg += f"- **{p.symbol}**: {p.qty} shares\n"
                msg += f"  Entry: ${float(p.avg_entry_price):,.2f} | Current: ${float(p.current_price):,.2f}\n"
                msg += f"  PnL: ${float(p.unrealized_pl):,.2f} ({float(p.unrealized_plpc)*100:.2f}%)\n\n"
                
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to fetch portfolio: {str(e)}")

async def analyze_cmd(update, context):
    asset = context.args[0] if context.args else "BTC"
    asset = asset.upper()
    
    await update.message.reply_text(f"📊 Running market intelligence on {asset}...")
    
    if asset in ["BTC", "ETH", "SOL"]:
        data = await asyncio.to_thread(get_crypto_data, asset)
        news = await asyncio.to_thread(search_web, f"{asset} crypto news today")
    else:
        data = await asyncio.to_thread(get_stock_data, asset)
        news = await asyncio.to_thread(search_web, f"{asset} stock news today")
        
    if "error" in data:
        return await update.message.reply_text(f"❌ {data['error']}")
        
    prompt = f"""
    You are a professional financial analyst. Analyze the following data for {asset}:
    
    TECHNICALS:
    - Price: {data['price']}
    - RSI (14): {data['rsi']} (Above 70 = Overbought, Below 30 = Oversold)
    - MACD Trend: {data['macd']}
    
    NEWS HEADLINES:
    {news}
    
    Based on this, provide:
    1. A clear verdict: BUY, SELL, or HOLD.
    2. A confidence score (1-10).
    3. A 3-bullet-point reasoning explaining the technicals and news sentiment.
    """
    
    reply = await asyncio.to_thread(ask_groq, [{"role": "user", "content": prompt}])
    await update.message.reply_text(reply)

async def trade_cmd(update, context):
    args = context.args
    if len(args) < 2:
        return await update.message.reply_text("Usage: /trade <asset> <amount>\nExample: /trade BTC 0.001")
    
    asset = args[0].upper()
    try:
        quantity = float(args[1])
    except ValueError:
        return await update.message.reply_text("Invalid amount. Please provide a number (e.g., 0.001).")
        
    await update.message.reply_text(f"📊 Analyzing {asset} for trade execution...")
    
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
        return await update.message.reply_text(f"❌ Analysis Failed: {data['error']}")
        
    prompt = f"""
    You are an expert trader. Analyze this data for {asset}:
    - Price: {data['price']}
    - RSI: {data['rsi']}
    - MACD: {data['macd']}
    
    Decide whether to BUY or SELL. Reply with ONLY ONE WORD: "BUY" or "SELL".
    """
    
    verdict = await asyncio.to_thread(ask_groq, [{"role": "user", "content": prompt}])
    verdict = verdict.strip().upper()
    
    if "BUY" in verdict:
        side = OrderSide.BUY
    elif "SELL" in verdict:
        side = OrderSide.SELL
    else:
        return await update.message.reply_text(f"🤖 AI verdict was {verdict}. No trade executed.")
        
    if side == OrderSide.SELL and is_crypto:
        position = await asyncio.to_thread(get_alpaca_position, symbol)
        if position < quantity:
            return await update.message.reply_text(
                f"⚠️ **Trade Blocked**\n\n"
                f"The AI verdict is **SELL**, but you only hold {position} {asset}.\n"
                f"Alpaca does not allow short-selling crypto. You can only sell what you own.\n\n"
                f"Try `/trade {asset} {quantity}` again later when the AI signals **BUY**, or acquire some {asset} first."
            )
            
    if side == OrderSide.BUY:
        balance = await asyncio.to_thread(get_alpaca_balance)
        cost = data['price'] * quantity
        if balance < cost:
            return await update.message.reply_text(
                f"⚠️ **Trade Blocked**\n\n"
                f"Insufficient USD balance.\n"
                f"Estimated cost: ${cost:,.2f} USD\n"
                f"Available: ${balance:,.2f} USD\n\n"
                f"Please lower the quantity and try again."
            )

    await update.message.reply_text(f"🤖 AI Verdict: {verdict}. Executing trade on Alpaca Paper...")
    
    try:
        order_data = MarketOrderRequest(
            symbol=symbol,
            qty=quantity,
            side=side,
            time_in_force=tif
        )
        order = await asyncio.to_thread(alpaca_client.submit_order, order_data=order_data)
        
        msg = f"✅ **Trade Executed Successfully**\n\n"
        msg += f"Symbol: {symbol}\n"
        msg += f"Side: {side}\n"
        msg += f"Quantity: {quantity}\n"
        msg += f"Order ID: {order.id}"
        
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"❌ Trade Failed: {str(e)}")

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
            
            video_url = await asyncio.to_thread(render_video, img_url, aud_url, script)
            await context.bot.send_message(chat_id, f"✅ Video Ready:\n{video_url}")
        except Exception as e:
            await context.bot.send_message(chat_id, f"Error: {e}")

    asyncio.create_task(process())

async def handle_msg(update, context):
    user_id = str(update.effective_user.id)
    text = update.message.text
    history = get_memory(user_id)
    
    url_match = re.search(r'https?://\S+', text)
    if url_match:
        await update.message.reply_text("🔗 Reading link...")
        content = await asyncio.to_thread(read_link, url_match.group(0))
        
        if "Error" in content or "Could not" in content:
            return await update.message.reply_text(f"❌ {content}")
            
        prompt = f"Here is the content from the link:\n\n{content}\n\nProvide a summary and key insights."
        reply = await asyncio.to_thread(ask_groq, [{"role": "user", "content": prompt}])
        await update.message.reply_text(reply)
        return
        
    history.append({"role": "user", "content": text})
    reply = await asyncio.to_thread(ask_groq, [{"role": "system", "content": "You are Hezzy's assistant."}] + history)
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))
    
    # Schedule the auto-trader to run every 5 minutes (300 seconds)
    job_queue = app.job_queue
    if job_queue:
        job_queue.run_repeating(auto_trade_job, interval=300, first=10)

    while True:
        try:
            print("Bot is running with Auto-Trader (5 min interval)...")
            app.run_polling(drop_pending_updates=True)
        except Exception as e:
            print(f"Restarting due to {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
 
