import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters
import urllib.request
import urllib.error
import json
from tavily import TavilyClient

# --- Keys ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

tavily = TavilyClient(api_key=TAVILY_API_KEY)

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
    print(f"Dummy web server running on port {port}")

# --- Search ---
def search_web(query):
    try:
        response = tavily.search(query=query, search_depth="basic", max_results=3)
        results = response.get('results', [])
        if not results:
            return "No results found."
        snippets = []
        for r in results:
            title = r.get('title', '')
            content = r.get('content', '')[:300]
            snippets.append(f"Title: {title}\nInfo: {content}")
        return "\n\n".join(snippets)
    except Exception as e:
        return f"SEARCH_ERROR: {str(e)[:100]}"

# --- AI ---
def ask_groq(prompt):
    url = "https://api.groq.com/openai/v1/chat/completions"
    data = {
        "model": "openai/gpt-oss-20b",
        "messages": [{"role": "user", "content": prompt}],
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
    await safe_reply(update, 'Hello Hezzy! I am running 24/7 on the cloud. Ask me anything.')

async def handle_message(update, context):
    user_text = update.message.text
    await safe_reply(update, "Searching the web...")
    search_results = search_web(user_text)
    if search_results.startswith("SEARCH_ERROR"):
        await safe_reply(update, f"Debug: {search_results}")
        return
    prompt = f"Answer the user's question using ONLY the information below. If the answer isn't there, say 'I couldn't find that.'\n\nInformation:\n{search_results}\n\nQuestion: {user_text}"
    reply = ask_groq(prompt)
    await safe_reply(update, reply)

def main():
    start_dummy_server()  # Start the web server for Render
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot is running 24/7...")
    app.run_polling(drop_pending_updates=True)  # <-- Added to prevent Conflict

if __name__ == "__main__":
    main()
