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
        print(f"MEMORY READ ERROR: {e}")
        return []

def save_memory(user_id, history):
    try:
        short_history = history[-8:] # Keep last 4 exchanges
        redis.set(f"memory:{user_id}", json.dumps(short_history))
    except Exception as e:
        print(f"MEMORY SAVE ERROR: {e}")

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
        "temperature": 0.5
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
            content = parsed['choices'][0]['message']['content']
            if not content or content.strip() == "":
                return "I'm sorry, I couldn't generate a response."
            return content
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
    await safe_reply(update, 'Hello Hezzy! I am awake. Ask me anything.')

async def handle_message(update, context):
    user_id = str(update.effective_user.id)
    user_text = update.message.text
    
    # 1. Load memory
    history = get_memory(user_id)
    
    # 2. Add the user's new message
    history.append({"role": "user", "content": user_text})
    
    # 3. Check if search is needed
    search_keywords = ["news", "latest", "today", "current", "price", "who won", "weather"]
    needs_search = any(keyword in user_text.lower() for keyword in search_keywords)
    
    # We build the message list with a SYSTEM PROMPT to tell the AI about its memory
    messages = [
        {"role": "system", "content": "You are Hezzy's personal assistant. You have a long-term memory database. The previous messages in this conversation are pulled from your memory. Use them to answer questions about the user. Be friendly and concise."}
    ] + history

    if needs_search:
        await safe_reply(update, "Searching the web...")
        search_results = search_web(user_text)
        messages.append({"role": "system", "content": f"Web Search Results: {search_results}"})
    
    # 4. Ask Groq
    reply = ask_groq(messages)
    
    # 5. Save to memory
    history.append({"role": "assistant", "content": reply})
    save_memory(user_id, history)
    
    await safe_reply(update, reply)

def main():
    start_dummy_server()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot is running 24/7 with Clean Memory...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
