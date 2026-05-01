import os
import asyncio
from yt_dlp import YoutubeDL
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

# ================= CONFIG =================
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"   # 🔹 Get this from @BotFather on Telegram
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ==========================================

# Progress Hook - Shows download progress in real-time
def progress_hook(loop, message):
    def inner(d):
        if d['status'] == 'downloading':
            percent = d.get('_percent_str', '').strip()
            speed = d.get('_speed_str', '0 KiB/s')
            eta = d.get('eta', 0)
            
            text = f"📥 Downloading...\nProgress: {percent}\nSpeed: {speed}\nETA: {eta}s"
            asyncio.run_coroutine_threadsafe(message.edit_text(text), loop)
        
        elif d['status'] == 'finished':
            asyncio.run_coroutine_threadsafe(
                message.edit_text("✅ Download finished! Preparing file..."), 
                loop
            )
    return inner


# Download video function
async def download_video(url, quality, message, loop):
    # Quality to height mapping
    quality_map = {
        "360": 360,
        "480": 480,
        "720": 720,
        "1080": 1080,
        "best": None  # No height limit for best
    }
    
    height = quality_map.get(quality)
    
    if height:
        format_selector = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"
    else:
        format_selector = "bestvideo+bestaudio/best"
    
    ydl_opts = {
        "format": format_selector,
        "progress_hooks": [progress_hook(loop, message)],
        "outtmpl": os.path.join(DOWNLOAD_DIR, "%(title)s_%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    
    def run_ydl():
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # Get the actual file path
            filename = ydl.prepare_filename(info)
            if not filename.endswith(".mp4"):
                filename = os.path.splitext(filename)[0] + ".mp4"
            return info, filename
    
    return await loop.run_in_executor(None, run_ydl)


# Start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 **YouTube Video Downloader Bot**\n\n"
        "Send me a YouTube link and I'll download it for you!\n\n"
        "Supported qualities: 360p, 480p, 720p, 1080p, Best\n\n"
        "Made with ❤️ using yt-dlp",
        parse_mode="Markdown"
    )


# Handle YouTube links
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    
    # Check if it's a YouTube link
    if "youtube.com" not in url.lower() and "youtu.be" not in url.lower():
        await update.message.reply_text("❌ Please send a valid YouTube link.")
        return
    
    # Save URL in context for later use
    context.user_data['url'] = url
    
    # Quality selection keyboard
    keyboard = [
        [
            InlineKeyboardButton("360p", callback_data="360"),
            InlineKeyboardButton("480p", callback_data="480")
        ],
        [
            InlineKeyboardButton("720p", callback_data="720"),
            InlineKeyboardButton("1080p", callback_data="1080")
        ],
        [
            InlineKeyboardButton("🎯 Best Quality", callback_data="best")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🎥 **Choose video quality:**",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )


# Handle quality selection
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    quality = query.data
    url = context.user_data.get('url')
    
    if not url:
        await query.edit_message_text("❌ Session expired. Please send the video link again.")
        return
    
    # Initial message
    await query.edit_message_text(f"🔄 Starting download in **{quality}p** quality...", parse_mode="Markdown")
    
    loop = asyncio.get_running_loop()
    
    try:
        info, filepath = await download_video(url, quality, query.message, loop)
        
        # Get file size
        file_size = os.path.getsize(filepath)
        size_mb = file_size / (1024 * 1024)
        
        # Telegram has a 50MB file size limit
        if size_mb > 50:
            await query.edit_message_text(
                f"❌ File too large ({size_mb:.1f}MB). Telegram free bots have a 50MB limit.\n\n"
                f"Try downloading in a lower quality or use the Streamlit web version."
            )
            os.remove(filepath)
            return
        
        # Send video
        await query.edit_message_text("📤 Uploading video to Telegram...")
        
        with open(filepath, "rb") as video_file:
            await query.message.reply_video(
                video=video_file,
                caption=f"✅ **Download Complete!**\n\n"
                        f"📹 **Title:** {info.get('title', 'Unknown')}\n"
                        f"🎬 **Quality:** {quality}p\n"
                        f"📦 **Size:** {size_mb:.1f}MB\n"
                        f"🔗 [Watch on YouTube]({url})",
                parse_mode="Markdown"
            )
        
        await query.edit_message_text("✅ Done! Video sent successfully.")
        
        # Clean up
        os.remove(filepath)
        
    except Exception as e:
        error_msg = str(e)
        if "ffmpeg" in error_msg.lower():
            await query.edit_message_text(
                "❌ **Error:** FFmpeg not installed on server.\n\n"
                "The server needs FFmpeg to merge video and audio.\n"
                "Try using a lower quality or contact the bot owner.",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text(f"❌ Error: {error_msg[:200]}")


# Help command
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 **How to use this bot:**\n\n"
        "1. Send me any YouTube video link\n"
        "2. Choose your preferred quality\n"
        "3. Wait for the download and upload\n"
        "4. Enjoy your video!\n\n"
        "⚠️ **Note:** Telegram limits file size to 50MB.\n"
        "For larger videos, use the web version.\n\n"
        "**Commands:**\n"
        "/start - Start the bot\n"
        "/help - Show this help message",
        parse_mode="Markdown"
    )


# Cancel command
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Operation cancelled. Send a new link when ready.")


# Main function
def main():
    # Create application
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Add handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))
    
    print("🤖 Bot is running... Press Ctrl+C to stop")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
