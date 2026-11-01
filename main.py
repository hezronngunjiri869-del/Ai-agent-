import os
import threading
import io
import tempfile
import asyncio
import time
import uuid
import urllib.request
import urllib.parse
import urllib.error
import json
import edge_tts
import shotstack_sdk as shotstack
from shotstack_sdk.api import edit_api
from shotstack_sdk.model.edit import Edit
from shotstack_sdk.model.timeline import Timeline
from shotstack_sdk.model.track import Track
from shotstack_sdk.model.clip import Clip
from shotstack_sdk.model.image_asset import ImageAsset
from shotstack_sdk.model.soundtrack import Soundtrack
from shotstack_sdk.model.output import Output
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import time, timezone
import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from tavily import TavilyClient
from upstash_redis import Redis

# --- Keys ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
UPSTASH_URL = os.environ.get("UPSTASH_URL", "")
UPSTASH_TOKEN = os.environ.get("UPSTASH_TOKEN", "")
SHOTSTACK_KEY = os.environ.get("SHOTSTACK_KEY", "")

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
    except Exception:
        return []

def save_memory(user_id, history):
    try:
        redis.set(f"memory:{user_id}", json.dumps(history[-12:]))
    except Exception:
        pass

# --- TOOLS ---
def get_crypto_price(coin="btc"):
    try:
        coin_map = {"btc": "btc-bitcoin", "eth": "eth-ethereum"}
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
        return f"{symbol}: ${usd_price:,.2f} USD (≈ KES {usd_price * kes_rate:,.2f})"
    except Exception:
        return "Market data unavailable."

def search_web(query):
    try:
        response = tavily.search(query=query, search_depth="basic", max_results=3)
        results = response.get('results', [])
        return "\n".join([f"- {r.get('title', '')}: {r.get('content', '')[:200]} (Link: {r.get('url', '')})" for r in results])
    except Exception:
        return "Search unavailable."

def find_jobs(skill):
    try:
        query = f'site:upwork.com OR site:freelancer.com "{skill}"'
        response = tavily.search(query=query, search_depth="advanced", max_results=3)
        results = response.get('results', [])
        if not results: return "No jobs found."
        return "\n\n".join([f"Title: {r.get('title')}\nSnippet: {r.get('content')[:150]}\nLink: {r.get('url')}" for r in results])
    except Exception as e:
        return f"Job Search Error: {str(e)}"

async def generate_audio(text):
    try:
        clean_text = text.replace("*", "").replace("#", "").replace("[", "").replace("]", "")
        voice = "en-US-BrianNeural" 
        communicate = edge_tts.Communicate(clean_text, voice)
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        await communicate.save(temp_file.name)
        with open(temp_file.name, "rb") as f:
            audio_bytes = f.read()
        os.unlink(temp_file.name)
        return audio_bytes
    except Exception as e:
        return None

def generate_image(prompt):
    try:
        clean_prompt = urllib.parse.quote(prompt)
        url = f"https://image.pollinations.ai/prompt/{clean_prompt}?width=1024&height=1024&nologo=true"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            return response.read()
    except Exception:
        return None

# --- FILE UPLOADER (For Shotstack) ---
def upload_to_tmpfiles(file_bytes, filename):
    try:
        # Using 0x0.st, a simple temporary file host
        boundary = '----WebKitFormBoundary' + str(uuid.uuid4())
        body = b''
        body += f'--{boundary}\r\n'.encode()
        body += f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
        body += b'Content-Type: application/octet-stream\r\n\r\n'
        body += file_bytes
        body += f'\r\n--{boundary}--\r\n'.encode()

        req = urllib.request.Request('https://0x0.st', data=body, method='POST')
        req.add_header('Content-Type', f'multipart/form-data; boundary={boundary}')
        req.add_header('User-Agent', 'Mozilla/5.0')

        with urllib.request.urlopen(req) as response:
            return response.read().decode('utf-8').strip()
    except Exception as e:
        print(f"Upload Error: {e}")
        return None

# --- SHOTSTACK VIDEO RENDERER ---
def render_video(image_url, audio_url, duration=15):
    try:
        host = "https://api.shotstack.io/stage" # Stage is the free sandbox
        configuration = shotstack.Configuration(host=host)
        configuration.api_key['DeveloperKey'] = SHOTSTACK_KEY

        with shotstack.ApiClient(configuration) as api_client:
            api_instance = edit_api.EditApi(api_client)

            # 1. Create Image Asset
            image_asset = shotstack.ImageAsset(src=image_url)
            image_clip = shotstack.Clip(asset=image_asset, start=0, length=duration)
            image_track = shotstack.Track(clips=[image_clip])

            # 2. Create Audio Asset
            soundtrack = shotstack.Soundtrack(src=audio_url, effect="fadeInFadeOut")

            # 3. Build Timeline
            timeline = shotstack.Timeline(
                background="#000000",
                tracks=[image_track],
                soundtrack=soundtrack
            )

            # 4. Output settings
            output = shotstack.Output(format="mp4", resolution="sd")

            # 5. Send to Shotstack
            edit = Edit(timeline=timeline, output=output)
            response = api_instance.post_render(edit)
            render_id = response['response']['id']
            
            # 6. Poll for completion
            for _ in range(30): # Try for 60 seconds
                time.sleep(2)
                status_response = api_instance.get_render(render_id, data=False)
                status = status_response['response']['status']
                if status == 'done':
                    return status_response['response']['url']
                elif status == 'failed':
                    return None
            return None
    except Exception as e:
        print(f"Shotstack Error: {e}")
        return None

# --- AI BRAIN ---
def ask_groq(messages):
    url = "https://api.groq.com/openai/v1/chat/completions"
    data = {"model": "openai/gpt-oss-20b", "messages": messages, "max_tokens": 1200, "temperature": 0.6}
    headers = {'Content-Type': 'application/json', 'Authorization': f'Bearer {GROQ_API_KEY}', 'User-Agent': 'HezzyAI/1.0'}
    req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req) as response:
            parsed = json.loads(response.read().decode('utf-8'))
            return parsed['choices'][0]['message']['content']
    except Exception:
        return "AI is currently thinking. Please try again."

def clean_text(text):
    return str(text).replace('\x00', '')[:4000] if text else "No response"

async def safe_reply(update, text):
    try: await update.message.reply_text(clean_text(text))
    except: pass

# --- AUTOMATED MORNING REPORT ---
async def send_morning_report(context: telegram.ext.CallbackContext):
    chat_id = redis.get("report_chat_id")
    if not chat_id: return
    btc = get_crypto_price("btc")
    eth = get_crypto_price("eth")
    news = search_web("latest technology news in Kenya today")
    report_text = f"🌅 Good Morning, Hezzy!\n\n📈 Markets:\n{btc}\n{eth}\n\n📰 Tech News:\n{news}"
    try: await context.bot.send_message(chat_id=int(chat_id), text=report_text)
    except Exception: pass

# --- HANDLERS ---
async def start(update, context):
    await safe_reply(update, 'Hello Hezzy! I am a full video studio. Use /video <topic> to generate a complete MP4 video with images, audio, and a script!')

async def video_command(update, context):
    topic = " ".join(context.args)
    if not topic:
        await safe_reply(update, "Please provide a topic. Example: /video AI tools in Kenya")
        return

    await safe_reply(update, f"🎬 **Starting Video Production for:** {topic}\n\n1️⃣ Writing script...")
    
    # 1. Generate Script
    script_prompt = f"Write a 15-second viral TikTok script about {topic}. Keep it punchy."
    script = ask_groq([{"role": "user", "content": script_prompt}])
    await safe_reply(update, f"**📝 Script:**\n\n{script}")
    
    # 2. Generate Assets
    await safe_reply(update, "2️⃣ Creating image and audio...")
    image_bytes = generate_image(f"TikTok background for a video about {topic}, cinematic")
    audio_bytes = await generate_audio(script)
    
    if not image_bytes or not audio_bytes:
        await safe_reply(update, "❌ Failed to generate image or audio.")
        return

    # 3. Upload Assets to Shotstack
    await safe_reply(update, "3️⃣ Uploading assets to the cloud...")
    image_url = upload_to_tmpfiles(image_bytes, "image.jpg")
    audio_url = upload_to_tmpfiles(audio_bytes, "audio.mp3")
    
    if not image_url or not audio_url:
        await safe_reply(update, "❌ Failed to upload assets to the cloud.")
        return

    # 4. Render Video
    await safe_reply(update, "4️⃣ Rendering your final video (this takes about 30 seconds)...")
    video_url = render_video(image_url, audio_url, duration=15)
    
    if video_url:
        await safe_reply(update, f"✅ **Video Ready!**\n\nClick here to download: {video_url}")
    else:
        await safe_reply(update, "❌ Shotstack failed to render the video. Check your API key or credits.")

async def set_report(update, context):
    chat_id = str(update.effective_chat.id)
    redis.set("report_chat_id", chat_id)
    await safe_reply(update, "✅ Done! I will send your Market & Tech report every morning at 8:00 AM.")

async def handle_message(update, context):
    user_id = str(update.effective_user.id)
    user_text = update.message.text
    lower_text = user_text.lower()
    
    history = get_memory(user_id)
    history.append({"role": "user", "content": user_text})
    
    # Standard tools fallback
    if "find me jobs" in lower_text:
        await safe_reply(update, "🕵️‍♂️ Scanning Upwork...")
        jobs_data = find_jobs("writing")
        reply = ask_groq([{"role": "system", "content": "Summarize jobs and write a proposal."}, {"role": "user", "content": jobs_data}])
        await safe_reply(update, reply)
        return

    if "price" in lower_text or "btc" in lower_text:
        await safe_reply(update, "Checking market...")
        history.append({"role": "system", "content": f"Market: {get_crypto_price('btc')} | {get_crypto_price('eth')}"})
    
    if "news" in lower_text:
        await safe_reply(update, "Searching web...")
        history.append({"role": "system", "content": f"News: {search_web(user_text)}"})

    reply = ask_groq([{"role": "system", "content": "You are Hezzy's assistant."}] + history)
    history.append({"role": "assistant", "content": reply})
    save_memory(user_id, history)
    await safe_reply(update, reply)

def main():
    start_dummy_server()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("report", set_report))
    app.add_handler(CommandHandler("video", video_command)) # The new Video command
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    job_queue = app.job_queue
    if job_queue:
        target_time = time(hour=5, minute=0, tzinfo=timezone.utc)
        job_queue.run_daily(send_morning_report, time=target_time)

    print("Bot is running with Shotstack Video Studio...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
