python-telegram-bot[job-queue]
tavily-python
upstash-redis
edge-tts
cloudinary
pandas
yfinance
alpaca-py
requests
pypdf
async def start(update, context):
    await update.message.reply_text("Bot is alive. Commands:\n/analyze <asset>\n/video <topic>\n/trade <asset> <amount>\n/portfolio\n/task <prompt>\n/clear_pdf\n/report\n\nYou can also upload a PDF to ask questions!")

async def set_report(update, context):
    redis.set("report_chat_id", str(update.effective_chat.id))
    await update.message.reply_text("✅ Auto-Trader activated! I will analyze and trade every hour with a 2% Stop Loss.")

async def portfolio_cmd(update, context):
    await update.message.reply_text("💼 Fetching your portfolio...")
    try:
        account = await asyncio.to_thread(alpaca_client.get_account)
        positions = await asyncio.to_thread(alpaca_client.get_all_positions)
        msg = f"📊 **Portfolio Summary**\n\n💰 Cash Balance: ${float(account.cash):,.2f}\n📈 Total Equity: ${float(account.equity):,.2f}\n\n"
        if not positions: msg += "No open positions."
        else:
            msg += "**Open Positions:**\n"
            for p in positions:
                msg += f"- **{p.symbol}**: {p.qty} shares\n  Entry: ${float(p.avg_entry_price):,.2f} | Current: ${float(p.current_price):,.2f}\n  PnL: ${float(p.unrealized_pl):,.2f} ({float(p.unrealized_plpc)*100:.2f}%)\n\n"
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {str(e)}")

async def analyze_cmd(update, context):
    asset = (context.args[0] if context.args else "BTC").upper()
    await update.message.reply_text(f"📊 Analyzing {asset}...")
    if asset in ["BTC", "ETH", "SOL"]:
        data = await asyncio.to_thread(get_crypto_data, asset)
        news = await asyncio.to_thread(search_web, f"{asset} crypto news")
    else:
        data = await asyncio.to_thread(get_stock_data, asset)
        news = await asyncio.to_thread(search_web, f"{asset} stock news")
    if "error" in data: return await update.message.reply_text(f"❌ {data['error']}")
    lessons = get_trading_lessons()
    prompt = f"Lessons: {lessons}\nAnalyze {asset}:\nPrice: {data['price']}\nRSI: {data['rsi']}\nMACD: {data['macd']}\nNews: {news}\nProvide: 1. Verdict (BUY/SELL/HOLD). 2. Confidence. 3. Reasoning."
    await update.message.reply_text(await asyncio.to_thread(ask_groq, [{"role": "user", "content": prompt}]))

async def task_cmd(update, context):
    task_prompt = " ".join(context.args)
    if not task_prompt:
        return await update.message.reply_text("Usage: /task <paste the prompt here>")
    await update.message.reply_text("🧠 Analyzing task and generating expert response...")
    system_prompt = "You are an elite AI data annotator. Complete the following task with perfect accuracy. Provide ONLY the final output requested without conversational filler."
    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": task_prompt}]
    reply = await asyncio.to_thread(ask_groq, messages, 2000)
    await update.message.reply_text(f"✅ **Task Complete:**\n\n{reply}")

async def trade_cmd(update, context):
    args = context.args
    if len(args) < 2: return await update.message.reply_text("Usage: /trade <asset> <amount>")
    asset = args[0].upper()
    try: quantity = float(args[1])
    except: return await update.message.reply_text("Invalid amount.")
    await update.message.reply_text(f"📊 Analyzing {asset}...")
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
        return await update.message.reply_text(f"❌ {data['error']}")
    lessons = get_trading_lessons()
    prompt = f"Lessons: {lessons}\nAnalyze {asset}: Price: {data['price']}, RSI: {data['rsi']}, MACD: {data['macd']}. Reply ONLY ONE WORD: 'BUY' or 'SELL'."
    verdict = await asyncio.to_thread(ask_groq, [{"role": "user", "content": prompt}])
    verdict = verdict.strip().upper()
    if "BUY" in verdict: side = OrderSide.BUY
    elif "SELL" in verdict: side = OrderSide.SELL
    else: return await update.message.reply_text(f"🤖 AI said {verdict}.")
    if side == OrderSide.SELL and is_crypto:
        position, _ = await asyncio.to_thread(get_alpaca_position, symbol)
        if position is None or position < quantity: 
            return await update.message.reply_text(f"⚠️ Blocked: Hold {position} {asset}.")
    if side == OrderSide.BUY:
        balance = await asyncio.to_thread(get_alpaca_balance)
        cost = data['price'] * quantity
        if balance < cost: 
            return await update.message.reply_text(f"⚠️ Blocked: Need ${cost:,.2f}.")
    try:
        await asyncio.to_thread(alpaca_client.submit_order, order_data=MarketOrderRequest(symbol=symbol, qty=quantity, side=side, time_in_force=tif))
        await update.message.reply_text(f"✅ Executed: {side} {quantity} {symbol}.")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {str(e)}")

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
            await context.bot.send_message(chat_id, f"✅ Video Ready:\n{await asyncio.to_thread(render_video, img_url, aud_url, script)}")
        except Exception as e:
            await context.bot.send_message(chat_id, f"Error: {e}")
    asyncio.create_task(process())

# --- PDF READER FEATURE ---
async def handle_pdf(update, context):
    user_id = str(update.effective_user.id)
    await update.message.reply_text("📄 Downloading and reading PDF... This may take a moment.")
    
    try:
        file = await update.message.document.get_file()
        temp_path = f"doc_{user_id}.pdf"
        await file.download_to_drive(temp_path)
        
        reader = PdfReader(temp_path)
        text = ""
        for page in reader.pages:
            text += page.extract_text() + "\n"
            if len(text) > 15000: # Limit to avoid Groq token limits
                break
                
        redis.set(f"pdf_ctx:{user_id}", text)
        await update.message.reply_text(f"✅ PDF loaded! ({len(reader.pages)} pages read).\n\nI am now ready to answer questions based on this document. Just ask!\n(Use /clear_pdf to remove it from memory).")
    except Exception as e:
        await update.message.reply_text(f"❌ Error reading PDF: {str(e)}")
    finally:
        if os.path.exists(temp_path): os.remove(temp_path)

async def clear_pdf_cmd(update, context):
    user_id = str(update.effective_user.id)
    redis.delete(f"pdf_ctx:{user_id}")
    await update.message.reply_text("🧹 PDF context cleared from memory.")

async def handle_voice(update, context):
    await update.message.reply_text("🎙️ Transcribing...")
    voice_file = await update.message.voice.get_file()
    temp_path = f"voice_{update.effective_user.id}.ogg"
    await voice_file.download_to_drive(temp_path)
    transcribed_text = await asyncio.to_thread(transcribe_voice, temp_path)
    if not transcribed_text or "Error" in transcribed_text:
        return await update.message.reply_text(f"❌ Could not understand: {transcribed_text}")
    await update.message.reply_text(f"🗣️ You said: \"{transcribed_text}\"")
    update.message.text = transcribed_text
    await handle_msg(update, context) 
    if os.path.exists(temp_path): os.remove(temp_path)

async def handle_msg(update, context):
    user_id = str(update.effective_user.id)
    text = update.message.text
    lower_text = text.lower()
    history = get_memory(user_id)
    
    if "portfolio" in lower_text or "balance" in lower_text or "positions" in lower_text:
        return await portfolio_cmd(update, context)
    if "analyze" in lower_text and len(lower_text.split()) > 1:
        context.args = lower_text.split()[1:]
        return await analyze_cmd(update, context)
    if "task" in lower_text and len(lower_text.split()) > 1:
        context.args = lower_text.split()[1:]
        return await task_cmd(update, context)
        
    url_match = re.search(r'https?://\S+', text)
    if url_match:
        await update.message.reply_text("🔗 Reading link...")
        content = await asyncio.to_thread(read_link, url_match.group(0))
        if "Error" in content or "Could not" in content: return await update.message.reply_text(f"❌ {content}")
        await update.message.reply_text(await asyncio.to_thread(ask_groq, [{"role": "user", "content": f"Content: {content}\nProvide summary."}]))
        return
        
    pdf_context = redis.get(f"pdf_ctx:{user_id}")
    if pdf_context:
        system_prompt = f"You are Hezzy's assistant. Use the following PDF content to answer the user's question accurately:\n\n--- PDF START ---\n{pdf_context}\n--- PDF END ---"
    else:
        system_prompt = "You are Hezzy's assistant."
        
    history.append({"role": "user", "content": text})
    reply = await asyncio.to_thread(ask_groq, [{"role": "system", "content": system_prompt}] + history)
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
    app.add_handler(CommandHandler("task", task_cmd))
    app.add_handler(CommandHandler("clear_pdf", clear_pdf_cmd))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))
    
    job_queue = app.job_queue
    if job_queue:
        job_queue.run_repeating(auto_trade_job, interval=3600, first=10)
        job_queue.run_repeating(train_bot_daily, interval=86400, first=3600)
        
    while True:
        try:
            print("Bot is running with PDF Reader...")
            app.run_polling(drop_pending_updates=True)
        except Exception as e:
            print(f"Restarting due to {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
