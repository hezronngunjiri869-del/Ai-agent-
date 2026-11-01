import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
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
        print(f"MEMORY ERROR: {e}")
        return []

def save_memory(user_id, history):
    try:
        redis.set(f"memory:{user_id}", json.dumps(history[-12:]))
    except Exception as e:
        print(f"MEMORY ERROR: {e}")

# --- MARKET TOOL (Switched to CryptoCompare) ---
def get_crypto_price(coin_symbol="BTC"):
    try:
        coin_map = {"btc": "BTC", "bitcoin": "BTC", "eth": "ETH", "ethereum": "ETH", "sol": "SOL", "solana": "SOL", "doge": "DOGE"}
        symbol = coin_map.get(coin_symbol.lower(), "BTC")
        
        url = f"https://min-api.cryptocompare.com/data/price?fsym={symbol}&tsyms=USD,KES"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode('utf-8'))
            if "USD" in data:
                usd = data.get('USD')
                kes = data.get('KES', 'N/A')
                return f"{symbol} Live Price: ${usd} USD (approx. {kes} KES)."
            return f"Could not find price for {symbol}."
    except Exception as e:
        return f"Market Tool Error: {str(e)[:100]}"

# --- SEARCH TOOL ---
def search_web(query):
    try:
        response = tavily.search(query=query, search_depth="basic", max_results=3)
        results = response.get('results', [])
        if not results: return "No results found."
        return "\n\n".join([f"Title: {r.get('title', '')}\nInfo: {r.get('content', '')[:300]}" for r in results])
    except Exception as e:
        return f"SEARCH_ERROR: {str(e)[:100]}"

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
    except Exception as e:
        return f"API_ERROR: {str(e)[:150]}"

def clean_text(text):
    return str(text).replace('\x00', '')[:4000] if text else "No response"

async def safe_reply(update, text):
    try: await update.message.reply_text(clean_text(text))
    except: pass

# --- HANDLERS ---
async def start(update, context):
    await safe_reply(update, 'Hello Hezzy! I am your multi-purpose agent. I can search the web, remember things, and check live market prices. What do you need?')

async def handle_message(update, context):
    user_id = str(update.effective_user.id)
    user_text = update.message.text
    lower_text = user_text.lower()
    
    history = get_memory(user_id)
    history.append({"role": "user", "content": user_text})
    
    tool_data = ""
    
    # --- TOOL ROUTING LOGIC ---
    market_keywords = ["price", "btc", "eth", "bitcoin", "solana", "crypto", "market"]
    if any(word in lower_text for word in market_keywords):
        await safe_reply(update, "Checking live market data...")
        target_coin = "bitcoin"
        for c in ["btc", "eth", "sol", "doge"]:
            if c in lower_text: target_coin = c
        tool_data += f"\n[Market Data]: {get_crypto_price(target_coin)}\n"
    
    search_keywords = ["news", "latest", "trend", "today", "current"]
    if any(word in lower_text for word in search_keywords):
        await safe_reply(update, "Searching the web...")
        tool_data += f"\n[Web Search]: {search_web(user_text)}\n"

    # --- BUILD PROMPT ---
    messages = [
        {"role": "system", "content": "You are Hezzy's autonomous agent. You have access to live market data and web search results provided in [Brackets]. Use this data to answer clearly. Be concise. If the market data contains an error, tell the user."}
    ] + history

    if tool_data:
        messages.append({"role": "system", "content": f"Tool Outputs: {tool_data}"})
    
    reply = ask_groq(messages)
    
    history.append({"role": "assistant", "content": reply})
    save_memory(user_id, history)
    
    await safe_reply(update, reply)

def main():
    start_dummy_server()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot is running with Tools...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
