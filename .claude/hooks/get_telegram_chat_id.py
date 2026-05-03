#!/usr/bin/env python3
"""
Utility to get Telegram Chat ID.

Usage:
1. Start a chat with your bot on Telegram
2. Send any message to the bot
3. Run this script:
   python3 get_telegram_chat_id.py --token "YOUR_BOT_TOKEN"

The script will display your Chat ID.
"""

import argparse
import json
import urllib.request
import urllib.error
import sys
import os


def get_bot_token():
    """Get bot token from args, env, or config file."""
    parser = argparse.ArgumentParser(description="Get Telegram Chat ID")
    parser.add_argument("--token", "-t", help="Bot token (or set TELEGRAM_BOT_TOKEN env)")
    parser.add_argument("--watch", "-w", action="store_true",
                        help="Watch for new messages (poll continuously)")
    parser.add_argument("--config", "-c",
                        default=os.path.expanduser("~/.config/claude/telegram.conf"),
                        help="Config file path")
    args = parser.parse_args()

    # Priority: CLI arg > env var > config file
    if args.token:
        return args.token, args.watch

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if token:
        return token, args.watch

    if os.path.exists(args.config):
        with open(args.config, "r") as f:
            for line in f:
                if line.startswith("BOT_TOKEN="):
                    return line.strip().split("=", 1)[1], args.watch

    print("Error: No bot token found.")
    print("Provide via --token, TELEGRAM_BOT_TOKEN env, or config file.")
    sys.exit(1)


def get_updates(token, offset=None):
    """Get updates from Telegram API."""
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    if offset:
        url += f"?offset={offset}"

    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read().decode())
            return data.get("result", [])
    except urllib.error.URLError as e:
        print(f"Error connecting to Telegram: {e}")
        return None


def get_me(token):
    """Get bot info."""
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read().decode())
            return data.get("result", {})
    except urllib.error.URLError:
        return None


def main():
    token, watch = get_bot_token()

    # Show bot info
    bot_info = get_me(token)
    if bot_info:
        print(f"\n🤖 Bot: @{bot_info.get('username', 'unknown')}")
        print(f"   Name: {bot_info.get('first_name', 'unknown')}")
    else:
        print("\n⚠️  Could not verify bot token. Check your token.")
        sys.exit(1)

    print("\n📱 To get your Chat ID:")
    print("   1. Open Telegram")
    print("   2. Search for your bot and start a chat")
    print("   3. Send any message to the bot")
    print("   4. This script will detect it\n")

    if watch:
        print("👀 Watching for messages... (Press Ctrl+C to stop)\n")
    else:
        print("🔍 Checking for recent messages...\n")

    last_offset = 0
    found_chats = {}

    try:
        while True:
            updates = get_updates(token, last_offset + 1 if last_offset else None)

            if updates is None:
                if not watch:
                    sys.exit(1)
                continue

            for update in updates:
                last_offset = update.get("update_id", 0)

                # Handle regular messages
                if "message" in update:
                    msg = update["message"]
                    chat = msg.get("chat", {})
                    chat_id = chat.get("id")
                    chat_type = chat.get("type", "unknown")
                    username = chat.get("username", "N/A")
                    first_name = chat.get("first_name", "")
                    last_name = chat.get("last_name", "")
                    full_name = f"{first_name} {last_name}".strip()

                    # Handle from user (for groups, this is who sent the message)
                    from_user = msg.get("from", {})
                    from_id = from_user.get("id")
                    from_username = from_user.get("username", "N/A")

                    if chat_id and chat_id not in found_chats:
                        found_chats[chat_id] = {
                            "type": chat_type,
                            "username": username,
                            "name": full_name or username,
                            "from_id": from_id,
                            "from_username": from_username,
                        }

                        if chat_type == "private":
                            print(f"✅ Found Chat ID: {chat_id}")
                            print(f"   Type: {chat_type}")
                            print(f"   Username: @{username}")
                            print(f"   Name: {full_name}")
                        else:
                            print(f"✅ Found Chat ID: {chat_id}")
                            print(f"   Type: {chat_type}")
                            print(f"   Title: {chat.get("title", "N/A")}")
                            print(f"   Your User ID: {from_id} (@{from_username})")

                        print(f"\n📝 Add to your config:")
                        print(f"   CHAT_ID={chat_id}")
                        print()

            if not watch:
                break

            import time
            time.sleep(1)

    except KeyboardInterrupt:
        if found_chats:
            print("\n\n📋 Summary of found Chat IDs:")
            for chat_id, info in found_chats.items():
                print(f"   {chat_id} ({info['type']}: {info['name']})")
        else:
            print("\n\n⚠️  No messages found. Make sure you've sent a message to your bot.")

    if not found_chats and not watch:
        print("⚠️  No messages found.")
        print("   Send a message to your bot and run again,")
        print("   or use --watch to wait for incoming messages.")


if __name__ == "__main__":
    main()
