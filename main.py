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

cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME", ""),
    api_key=os.environ.get("CLOUDINARY_API_KEY", ""),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET", "")
)

tavily = TavilyClient(api_key=TAVILY_API_KEY)
redis = Redis(url=UPSTASH_URL, token=UPSTASH_TOKEN)

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

# --- AI (FIXED ERROR REPORTING) ---
def ask_groq(messages):
    url = "https://api.groq.com/openai/v1/chat/completions"
    data = {"model": "openai/gpt-oss-20b", "messages": messages, "max_tokens": 1000}
    headers = {'Content-Type': 'application/json', 'Authorization': f'Bearer {GROQ_API_KEY}'}
    req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode('utf-8'))['choices'][0]['message']['content']
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8')
        return f"Groq API Error {e.code}:\n{error_body[:300]}"
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
            # Limit to 1500 characters to prevent Token Limit errors
            return content[:1500] 
    except Exception as e:
        return f"Extraction Error: {str(e)}"
    return "Could not extract text from this site."

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

# --- TELEGRAM HANDLERS ---
async def start(update, context):
    await update.message.reply_text("Bot is alive. Send a link, ask a question, or use /video <topic>.")

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
            
        prompt = f"Here is the text from a link:\n\n{content}\n\nProvide a brief summary and key insights."
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
    app.add_handler(CommandHandler("video", video_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))
    
    while True:
        try:
            print("Bot is running...")
            app.run_polling(drop_pending_updates=True)
        except Exception as e:
            print(f"Restarting due to {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
