import os
import threading
import io
import tempfile
import asyncio
import time
import uuid
import datetime
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
from shotstack_sdk.model.title_asset import TitleAsset
from shotstack_sdk.model.soundtrack import Soundtrack
from shotstack_sdk.model.output import Output
from http.server import BaseHTTPRequestHandler, HTTPServer
import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from telegram.request import HTTPXRequest # Added for timeout fix
from tavily import TavilyClient
from upstash_redis import Redis
import cloudinary
import cloudinary.uploader
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# --- Keys ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
UPSTASH_URL = os.environ.get("UPSTASH_URL", "")
UPSTASH_TOKEN = os.environ.get("UPSTASH_TOKEN", "")
SHOTSTACK_KEY = os.environ.get("SHOTSTACK_KEY", "")
TIKTOK_ACCESS_TOKEN = os.environ.get("TIKTOK_ACCESS_TOKEN", "")
YOUTUBE_ACCESS_TOKEN = os.environ.get("YOUTUBE_ACCESS_TOKEN", "")

# --- Cloudinary Config ---
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME", ""),
    api_key=os.environ.get("CLOUDINARY_API_KEY", ""),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET", "")
)

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
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode('utf-8'))
            usd_price = float(data['quotes']['USD']['price'])
            symbol = data['symbol']

        fx_url = "https://open.er-api.com/v6/latest/USD"
        req_fx = urllib.request.Request(fx_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req_fx, timeout=15) as response:
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
    except Exception:
        return None

def generate_image(prompt):
    try:
        clean_prompt = urllib.parse.quote(prompt)
        url = f"https://image.pollinations.ai/prompt/{clean_prompt}?width=1024&height=1024&nologo=true"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.read()
    except Exception:
        return None

def upload_to_cloudinary(file_bytes, filename):
    try:
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=f"_{filename}")
        temp_file.write(file_bytes)
        temp_file.close()
        result = cloudinary.uploader.upload(temp_file.name, resource_type="auto")
        os.unlink(temp_file.name)
        return result['secure_url']
    except Exception as e:
        return f"Cloudinary Upload Error: {str(e)}"

def render_video(image_url, audio_url, topic, duration=20.0):
    try:
        host = "https://api.shotstack.io/stage"
        configuration = shotstack.Configuration(host=host)
        configuration.api_key['DeveloperKey'] = SHOTSTACK_KEY

        with shotstack.ApiClient(configuration) as api_client:
            api_instance = edit_api.EditApi(api_client)

            image_asset = ImageAsset(src=image_url)
            image_clip = Clip(asset=image_asset, start=0.0, length=float(duration))
            image_track = Track(clips=[image_clip])

            title_asset = TitleAsset(text=topic.upper(), style="minimal", size="large", position="center")
            title_clip = Clip(asset=title_asset, start=0.0, length=float(duration))
            title_track = Track(clips=[title_clip])

            soundtrack = Soundtrack(src=audio_url, effect="fadeInFadeOut")

            timeline = Timeline(background="#000000", tracks=[image_track, title_track], soundtrack=soundtrack)
            output = Output(format="mp4", resolution="sd")

            edit = Edit(timeline=timeline, output=output)
            response = api_instance.post_render(edit)
            render_id = response['response']['id']
            
            status_url = f"{host}/render/{render_id}"
            req = urllib.request.Request(status_url, headers={'x-api-key': SHOTSTACK_KEY})
            
            for _ in range(60):
                time.sleep(2)
                try:
                    with urllib.request.urlopen(req, timeout=15) as res:
                        data = json.loads(res.read().decode('utf-8'))
                        status = data['response']['status']
                        
                        if status == 'done':
                            return data['response']['url']
                        elif status == 'failed':
                            return f"Shotstack Error: {data['response'].get('error', 'Unknown Error')}"
                except Exception:
                    continue
            return "Render timed out."
    except Exception as e:
        return f"Shotstack Exception: {str(e)}"

def ask_groq(messages):
    url = "https://api.groq.com/openai/v1/chat/completions"
    data = {"model": "openai/gpt-oss-20b", "messages": messages, "max_tokens": 1200, "temperature": 0.6}
    headers = {'Content-Type': 'application/json', 'Authorization': f'Bearer {GROQ_API_KEY}', 'User-Agent': 'HezzyAI/1.0'}
    req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            parsed = json.loads(response.read().decode('utf-8'))
            return parsed['choices'][0]['message']['content']
    except Exception:
        return "AI error."

# --- AUTO-POSTING FUNCTIONS ---
def post_to_tiktok(video_url, caption):
    try:
        init_url = "https://open.tiktokapis.com/v2/post/publish/video/init/"
        payload = {
            "post_info": {"title": caption, "privacy_level": "SELF_ONLY", "is_aigc": True},
            "source_info": {"source": "PULL_FROM_URL", "video_url": video_url}
        }
        req = urllib.request.Request(
            init_url,
            data=json.dumps(payload).encode('utf-8'),
            headers={'Authorization': f'Bearer {TIKTOK_ACCESS_TOKEN}', 'Content-Type': 'application/json'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            return "TikTok posted successfully (Private Draft)."
    except Exception as e:
        return f"TikTok Error: {str(e)}"

def post_to_youtube(video_url, title, description):
    try:
        temp_video = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        urllib.request.urlretrieve(video_url, temp_video.name)
        temp_video.close()
        
        creds = Credentials(token=YOUTUBE_ACCESS_TOKEN)
        youtube = build('youtube', 'v3', credentials=creds)
        
        body = {
            'snippet': {'title': title[:100], 'description': description[:5000], 'categoryId': '22'},
            'status': {'privacyStatus': 'public'}
        }
        
        media = MediaFileUpload(temp_video.name, chunksize=-1, resumable=True)
        request = youtube.videos().insert(part='snippet,status', body=body, media_body=media)
        response = request.execute()
        
        os.unlink(temp_video.name)
        return f"YouTube posted: https://youtu.be/{response['id']}"
    except Exception as e:
        return f"YouTube Error: {str(e)}"

def clean_text(text):
    return str(text).replace('\x00', '')[:4000] if text else "No response"

async def safe_reply(update, text):
    try: await update.message.reply_text(clean_text(text))
    except: pass

# --- BACKGROUND VIDEO WORKER ---
async def process_video_background(update, context, topic):
    chat_id = update.effective_chat.id
    try:
        prompt = f"Write a 15-second viral TikTok script about {topic}. It must be around 35 to 40 words maximum. Keep it punchy."
        script = await asyncio.to_thread(ask_groq, [{"role": "user", "content": prompt}])
        
        image_bytes = await asyncio.to_thread(generate_image, f"TikTok background for a video about {topic}, cinematic")
        audio_bytes = await generate_audio(script)
        
        if not image_bytes or not audio_bytes:
            await context.bot.send_message(chat_id=chat_id, text="❌ Failed to generate image or audio internally.")
            return

        image_url = await asyncio.to_thread(upload_to_cloudinary, image_bytes, "image.jpg")
        audio_url = await asyncio.to_thread(upload_to_cloudinary, audio_bytes, "audio.mp3")
        
        if not image_url.startswith("http") or not audio_url.startswith("http"):
            await context.bot.send_message(chat_id=chat_id, text=f"❌ Upload Failed.")
            return

        video_url = await asyncio.to_thread(render_video, image_url, audio_url, topic, duration=20.0)
        
        if video_url.startswith("http"):
            await context.bot.send_message(chat_id=chat_id, text=f"✅ **Video Ready!**\n\n{video_url}")
        else:
            await context.bot.send_message(chat_id=chat_id, text=f"❌ Render Failed: {video_url}")
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Background Video Error: {str(e)}")

# --- AUTOMATED MORNING REPORT ---
async def send_morning_report(context: telegram.ext.CallbackContext):
    try:
        chat_id = redis.get("report_chat_id")
        if not chat_id: return
        btc = get_crypto_price("btc")
        eth = get_crypto_price("eth")
        news = search_web("latest technology news in Kenya today")
        summary_prompt = f"Here are raw search results about Kenyan tech news: {news}. Write a clean, 3-bullet-point summary. Do not include random characters."
        clean_news = ask_groq([{"role": "user", "content": summary_prompt}])
        report_text = f"🌅 Good Morning, Hezzy!\n\n📈 Markets:\n{btc}\n{eth}\n\n📰 Tech News:\n{clean_news}"
        await context.bot.send_message(chat_id=int(chat_id), text=report_text)
    except Exception as e:
        print(f"Report failed: {e}")

# --- ERROR HANDLER ---
async def error_handler(update, context):
    print(f"⚠️ ERROR: {context.error}")

# --- HANDLERS ---
async def start(update, context):
    await safe_reply(update, 'Hello Hezzy! Commands:\n/video <topic>\n/post tiktok|youtube <topic>\n/report')

async def video_command(update, context):
    topic = " ".join(context.args)
    if not topic:
        await safe_reply(update, "Example: /video AI tools in Kenya")
        return
    await safe_reply(update, f"🎬 Starting video for: {topic}")
    asyncio.create_task(process_video_background(update, context, topic))

async def post_video_command(update, context):
    args = context.args
    if len(args) < 2:
        await safe_reply(update, "Usage: /post tiktok|youtube <topic>")
        return
    
    platform = args[0].lower()
    topic = " ".join(args[1:])
    
    await safe_reply(update, f"🎬 Generating and preparing to post to {platform}...")
    
    script = await asyncio.to_thread(ask_groq, [{"role": "user", "content": f"Write a 15-second viral script about {topic}."}])
    image_bytes = await asyncio.to_thread(generate_image, f"TikTok background about {topic}")
    audio_bytes = await generate_audio(script)
    
    image_url = await asyncio.to_thread(upload_to_cloudinary, image_bytes, "image.jpg")
    audio_url = await asyncio.to_thread(upload_to_cloudinary, audio_bytes, "audio.mp3")
    video_url = await asyncio.to_thread(render_video, image_url, audio_url, topic)
    
    if not video_url.startswith("http"):
        await safe_reply(update, f"❌ Render failed: {video_url}")
        return
    
    await safe_reply(update, f"📤 Video ready. Posting to {platform}...")
    
    if platform == "tiktok":
        result = await asyncio.to_thread(post_to_tiktok, video_url, script)
    elif platform == "youtube":
        result = await asyncio.to_thread(post_to_youtube, video_url, topic, script)
    else:
        result = "Unknown platform. Use 'tiktok' or 'youtube'."
    
    await safe_reply(update, result)

async def set_report(update, context):
    chat_id = str(update.effective_chat.id)
    redis.set("report_chat_id", chat_id)
    await safe_reply(update, "✅ Done! I will send your Market & Tech report every morning.")

async def handle_message(update, context):
    try:
        user_id = str(update.effective_user.id)
        user_text = update.message.text
        lower_text = user_text.lower()
        
        history = get_memory(user_id)
        history.append({"role": "user", "content": user_text})
        
        if "find me jobs" in lower_text or "find jobs" in lower_text:
            await safe_reply(update, "🕵️‍♂️ Scanning Upwork...")
            jobs_data = await asyncio.to_thread(search_web, "freelance jobs " + user_text)
            reply = await asyncio.to_thread(ask_groq, [{"role": "system", "content": "Summarize jobs and write a proposal."}, {"role": "user", "content": jobs_data}])
            await safe_reply(update, reply)
            return

        if "price" in lower_text or "btc" in lower_text:
            await safe_reply(update, "Checking market...")
            market_info = await asyncio.to_thread(get_crypto_price, "btc")
            history.append({"role": "system", "content": f"Market: {market_info}"})
        
        if "news" in lower_text:
            await safe_reply(update, "Searching web...")
            news_info = await asyncio.to_thread(search_web, user_text)
            history.append({"role": "system", "content": f"News: {news_info}"})

        reply = await asyncio.to_thread(ask_groq, [{"role": "system", "content": "You are Hezzy's assistant."}] + history)
        history.append({"role": "assistant", "content": reply})
        save_memory(user_id, history)
        await safe_reply(update, reply)
    except Exception as e:
        await safe_reply(update, f"❌ Error: {str(e)}")

async def post_init(application):
    await application.bot.delete_webhook(drop_pending_updates=True)

def main():
    start_dummy_server()
    
    # FIX: Increase Telegram connection timeouts to prevent TimedOut errors
    custom_request = HTTPXRequest(connection_pool_size=8, connect_timeout=30.0, read_timeout=30.0)
    
    app = Application.builder().token(TELEGRAM_TOKEN).request(custom_request).post_init(post_init).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("report", set_report))
    app.add_handler(CommandHandler("video", video_command))
    app.add_handler(CommandHandler("post", post_video_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    
    job_queue = app.job_queue
    if job_queue:
        target_time = datetime.time(hour=5, minute=0, tzinfo=datetime.timezone.utc)
        job_queue.run_daily(send_morning_report, time=target_time)

    print("Bot is running with Timeout Fix...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
