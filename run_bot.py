# // Entrypoint for the Telegram Torrent Downloader Bot
import sys
import os

# Ensure the parent directory is in the path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from bot.api_control import main

if __name__ == "__main__":
    main()
