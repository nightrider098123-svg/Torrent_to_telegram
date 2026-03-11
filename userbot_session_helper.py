import sys
from pyrogram import Client

def main():
    print("=== Pyrogram Session String Generator ===")
    print("You will need your API ID and API Hash from https://my.telegram.org/apps")
    print("If you don't have them, please create an application there first.\n")

    api_id = input("Enter your API ID: ").strip()
    api_hash = input("Enter your API Hash: ").strip()

    if not api_id or not api_hash:
        print("API ID and API Hash are required.")
        sys.exit(1)

    try:
        api_id = int(api_id)
    except ValueError:
        print("API ID must be an integer.")
        sys.exit(1)

    # Initialize Pyrogram client in memory
    app = Client("session_generator", api_id=api_id, api_hash=api_hash, in_memory=True)

    with app:
        session_string = app.export_session_string()
        print("\n=== SUCCESS ===")
        print("Your session string has been generated successfully.\n")
        print("Save the following string in your config.cfg under the [MTProto] section as 'session_string':\n")
        print(session_string)
        print("\nWARNING: Keep this string secret! It gives full access to your Telegram account.")

if __name__ == "__main__":
    main()
