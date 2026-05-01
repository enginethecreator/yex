import os
import yt_dlp
from flask import Flask, request, jsonify
import logging

# Disable yt-dlp's default logger to avoid clutter
logging.getLogger('yt-dlp').disabled = True

# 1. Setup Flask App
app = Flask(__name__)

# 2. Get your bot token from Railway's environment variables
BOT_TOKEN = "8705048790:AAFH67hJcn1uLNc2OxL4TIk1xD46zVDny0A"
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set on Railway!")

# 3. Define the route Telegram will call
@app.route(f'/webhook/{BOT_TOKEN}', methods=['POST'])
def webhook():
    """Receives updates from Telegram."""
    try:
        update_dict = request.get_json()
        if not update_dict or 'message' not in update_dict:
            return jsonify({"status": "ok"}), 200

        message = update_dict['message']
        chat_id = message['chat']['id']
        text = message.get('text', '')

        # Handle the /start command
        if text == '/start':
            send_message(chat_id, "👋 Hello! Send me a YouTube link and I will download it for you.")
        # Handle YouTube links
        elif 'youtube.com' in text or 'youtu.be' in text:
            send_message(chat_id, "📥 Downloading, please wait...")
            download_and_send_video(chat_id, text)
        else:
            send_message(chat_id, "❌ Please send a valid YouTube link.")

        return jsonify({"status": "ok"}), 200
    except Exception as e:
        print(f"Error in webhook: {e}")
        return jsonify({"status": "error"}), 500

def send_message(chat_id, text):
    """Helper function to send a text message back to the user."""
    import requests
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Failed to send message: {e}")

def download_and_send_video(chat_id, url):
    """Downloads the video and uploads it to the user."""
    import requests
    unique_filename = f"{chat_id}_video.mp4"
    ydl_opts = {
        'format': 'best[ext=mp4]/best',
        'outtmpl': unique_filename,
        'quiet': True,
        'no_warnings': True,
    }
    try:
        # 1. Download the video using yt-dlp
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        
        # 2. Send the video file to the user
        with open(unique_filename, 'rb') as f:
            files = {'video': f}
            data = {'chat_id': chat_id}
            send_video_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo"
            requests.post(send_video_url, data=data, files=files, timeout=60)
    except Exception as e:
        send_message(chat_id, f"❌ An error occurred: {e}")
    finally:
        # 3. Clean up the file from the server
        if os.path.exists(unique_filename):
            os.remove(unique_filename)

# 4. A simple homepage to check if the server is running
@app.route('/')
def home():
    return "Telegram Bot for YouTube Downloads is running!"

# This is for local testing. On Railway, 'gunicorn app:app' handles it.
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
