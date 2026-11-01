import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import time
import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters
import urllib.request
import urllib.error
import json
from tavily import TavilyClient
from upstash_redis import Redis

# --- Keys ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
UPSTASH_URL = os.environ.get("UPSTASH_URL", "")
UPSTASH_TOKEN = os.environ.get("UPSTASH_TOKEN", "")

tavily = TavilyClient(api_key=TAVILY_API_KEY)
redis = Redis(url=UPSTASH_URL, token=UPSTASH_TOKEN)

# --- Dummy Web Server ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def start_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

# --- MEMORY FUNCTIONS ---
def get_memory(user_id):
    try:
        data = redis.get(f"memory:{user_id}")
        return json.loads(data) if data else []
    except Exception as e:
        return []

def save_memory(user_id, history):
    try:
        redis.set(f"memory:{user_id}", json.dumps(history[-12:]))
    except Exception:
        pass

# --- MARKET & SEARCH TOOLS ---
def get_crypto_price(coin="btc"):
    try:
        coin_map = {"btc": "btc-bitcoin", "eth": "eth-ethereum", "sol": "sol-solana"}
        coin_id = coin_map.get(coin.lower(), "btc-bitcoin")
        url = f"https://api.coinpaprika.com/v1/tickers/{coin_id}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode('utf-8'))
            usd_price = float(data['quotes']['USD']['price'])
            symbol = data['symbol']

        fx_url = "https://open.er-api.com/v6/latest/USD"
        req_fx = urllib.request.Request(fx_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req_fx) as response:
            fx_data = json.loads(response.read().decode('utf-8'))
            kes_rate = fx_data['rates']['KES']

        kes_price = usd_price * kes_rate
        return f"{symbol}: ${usd_price:,.2f} USD (≈ KES {kes_price:,.2f})"
    except Exception as e:
        return f"Error fetching {coin}: {str(e)[:50]}"

def search_web(query):
    try:
        response = tavily.search(query=query, search_depth="basic", max_results=3)
        results = response.get('results', [])
        return "\n".join([f"- {r.get('title', '')}: {r.get('content', '')[:200]}" for r in results])
    except Exception:
        return "Search unavailable."

# --- AI BRAIN ---
def ask_groq(messages):
    url = "https://api.groq.com/openai/v1/chat/completions"
    data = {"model": "openai/gpt-oss-20b", "messages": messages, "max_tokens": 600, "temperature": 0.4}
    headers = {'Content-Type': 'application/json', 'Authorization': f'Bearer {GROQ_API_KEY}', 'User-Agent': 'HezzyAI/1.0'}
    req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req) as response:
            parsed = json.loads(response.read().decode('utf-8'))
            return parsed['choices'][0]['message']['content']
    except Exception:
        return "AI error."

def clean_text(text):
    return str(text).replace('\x00', '')[:4000] if text else "No response"

async def safe_reply(update, text):
    try: await update.message.reply_text(clean_text(text))
    except: pass

# --- THE AUTOMATED MORNING REPORT ---
async def send_morning_report(context: telegram.ext.CallbackContext):
    # 1. Find out who to send the report to
    chat_id = redis.get("report_chat_id")
    if not chat_id:
        print("No chat ID saved for morning report.")
        return

    # 2. Fetch Data
    btc = get_crypto_price("btc")
    eth = get_crypto_price("eth")
    news = search_web("latest technology news in Kenya today")

    # 3. Format the message
    report_text = f"""🌅 **Good Morning, Hezzy! Here is your Daily Briefing:**

📈 **Market Watch:**
{btc}
{eth}

📰 **Kenyan Tech News:**
{news}

Have a productive day! 🚀"""

    # 4. Send it
    try:
        await context.bot.send_message(chat_id=int(chat_id), text=report_text)
        print("Morning report sent!")
    except Exception as e:
        print(f"Failed to send morning report: {e}")

# --- HANDLERS ---
async def start(update, context):
    await safe_reply(update, 'Hello Hezzy! Type /report to receive your automated morning market briefing at 8:00 AM EAT daily!')

async def set_report(update, context):
    # Save the user's chat ID to Upstash for the daily report
    chat_id = str(update.effective_chat.id)
    redis.set("report_chat_id", chat_id)
    await safe_reply(update, "✅ Done! I will send you the automated Market & Tech report every morning at 8:00 AM EAT.")

async def handle_message(update, context):
    user_id = str(update.effective_user.id)
    user_text = update.message.text
    
    history = get_memory(user_id)
    history.append({"role": "user", "content": user_text})
    
    # Quick tool checks
    if "price" in user_text.lower() or "btc" in user_text.lower():
        await safe_reply(update, "Checking market...")
        history.append({"role": "system", "content": f"Market: {get_crypto_price('btc')} | {get_crypto_price('eth')}"})
    
    reply = ask_groq([{"role": "system", "content": "You are Hezzy's agent."}] + history)
    history.append({"role": "assistant", "content": reply})
    save_memory(user_id, history)
    await safe_reply(update, reply)

def main():
    start_dummy_server()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("report", set_report))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Set up the daily timer (8:00 AM EAT = 5:00 AM UTC)
    job_queue = app.job_queue
    if job_queue:
        # time(hour, minute, second, microsecond, tzinfo)
        import datetime
        target_time = datetime.time(hour=5, minute=0, tzinfo=datetime.timezone.utc)
        job_queue.run_daily(send_morning_report, time=target_time)
        print("Daily report scheduled for 8:00 AM EAT.")

    print("Bot is running with Automated Morning Reports...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
