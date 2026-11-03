import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import telegram
from telegram.ext import Application, CommandHandler

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def start_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

async def start(update, context):
    await update.message.reply_text('I am alive and working!')

async def post_init(application):
    await application.bot.delete_webhook(drop_pending_updates=True)

def main():
    start_dummy_server()
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    
    print("TEST BOT IS RUNNING...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
