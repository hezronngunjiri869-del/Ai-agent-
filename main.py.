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

# --- Initialize Clients ---
tavily = TavilyClient(api_key=TAVILY_API_KEY)
redis = Redis(url=UPSTASH_URL, token=UPSTASH_TOKEN)

# --- Dummy Web Server for Render ---
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
        if data:
            return json.loads(data)
        return []
    except Exception as e:
        print(f"Memory read error: {e}")
        return []

def save_memory(user_id, history):
    try:
        # Keep only the last 6 messages to save space and tokens
        short_history = history[-6:]
        redis.set(f"memory:{user_id}", json.dumps(short_history))
    except Exception as e:
        print(f"Memory save error: {e}")

# --- Search ---
def search_web(query):
    try:
        response = tavily.search(query=query, search_depth="basic", max_results=3)
        results = response.get('results', [])
        if not results: return "No results found."
        snippets = []
        for r in results:
            snippets.append(f"Title: {r.get('title', '')}\nInfo: {r.get('content', '')[:300]}")
        return "\n\n".join(snippets)
    except Exception as e:
        return f"SEARCH_ERROR: {str(e)[:100]}"

# --- AI ---
def ask_groq(messages):
    url = "https://api.groq.com/openai/v1/chat/completions"
    data = {
        "model": "openai/gpt-oss-20b",
        "messages": messages,
        "max_tokens": 500,
        "temperature": 0.3
    }
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {GROQ_API_KEY}',
        'User-Agent': 'HezzyAI/1.0'
    }
    body = json.dumps(data).encode('utf-8')
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req) as response:
            parsed = json.loads(response.read().decode('utf-8'))
            return parsed['choices'][0]['message']['content']
    except urllib.error.HTTPError as e:
        return f"API_ERROR: {e.read().decode('utf-8')[:200]}"
    except Exception as e:
        return f"ERROR: {str(e)[:150]}"

def clean_text(text):
    if not text: return "No response"
    return str(text).replace('\x00', '')[:4000]

async def safe_reply(update, text):
    try:
        await update.message.reply_text(clean_text(text))
    except:
        pass

# --- Handlers ---
async def start(update, context):
    await safe_reply(update, 'Hello Hezzy! I now have permanent memory. Ask me anything.')

async def handle_message(update, context):
    user_id = str(update.effective_user.id)
    user_text = update.message.text
    
    # 1. Load memory
    history = get_memory(user_id)
    
    await safe_reply(update, "Searching the web...")
    search_results = search_web(user_text)
    
    # 2. Build the prompt
    prompt = f"Use this info to answer the question. If you can't, say so.\n\nInfo:\n{search_results}\n\nQuestion: {user_text}"
    
    # 3. Add user message to history and ask Groq
    history.append({"role": "user", "content": prompt})
    reply = ask_groq(history)
    
    # 4. Save AI response to history
    history.append({"role": "assistant", "content": reply})
    save_memory(user_id, history)
    
    await safe_reply(update, reply)

def main():
    start_dummy_server()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot is running 24/7 with Memory...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
