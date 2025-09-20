#!/usr/bin/env python3
"""
Complete Twitter-Telegram Bot - Now with working commands + Twitter monitoring
"""

import asyncio
import json
import os
import logging
from typing import List, Optional
from dataclasses import dataclass
from datetime import datetime
import aiohttp
import aiofiles
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Load .env if available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[logging.FileHandler('bot.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

@dataclass
class TwitterAccount:
    username: str
    user_id: str
    last_tweet_id: Optional[str] = None

class ConfigManager:
    """Manages bot configuration"""
    
    def __init__(self, config_file: str = "config.json"):
        self.config_file = config_file
        self.config = self._load_config()
    
    def _load_config(self) -> dict:
        default_config = {
            "monitored_accounts": [],
            "check_interval": 60
        }
        
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    config = json.load(f)
                    return {**default_config, **config}
            except Exception as e:
                logger.error(f"Error loading config: {e}")
        
        return default_config
    
    async def save_config(self):
        try:
            async with aiofiles.open(self.config_file, 'w') as f:
                await f.write(json.dumps(self.config, indent=2))
        except Exception as e:
            logger.error(f"Error saving config: {e}")
    
    def add_account(self, username: str, user_id: str) -> bool:
        if any(acc["username"].lower() == username.lower() 
               for acc in self.config["monitored_accounts"]):
            return False
        
        self.config["monitored_accounts"].append({
            "username": username,
            "user_id": user_id,
            "last_tweet_id": None
        })
        return True
    
    def remove_account(self, username: str) -> bool:
        original_count = len(self.config["monitored_accounts"])
        self.config["monitored_accounts"] = [
            acc for acc in self.config["monitored_accounts"]
            if acc["username"].lower() != username.lower()
        ]
        return len(self.config["monitored_accounts"]) < original_count
    
    def get_accounts(self) -> List[TwitterAccount]:
        return [TwitterAccount(**acc) for acc in self.config["monitored_accounts"]]
    
    def update_last_tweet(self, username: str, tweet_id: str):
        for acc in self.config["monitored_accounts"]:
            if acc["username"].lower() == username.lower():
                acc["last_tweet_id"] = tweet_id
                break

class TwitterMonitor:
    """Twitter API monitoring"""
    
    def __init__(self, bearer_token: str):
        self.bearer_token = bearer_token
        self.base_url = "https://api.twitter.com/2"
        self.headers = {"Authorization": f"Bearer {bearer_token}"}
        self.session = None
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            headers=self.headers,
            timeout=aiohttp.ClientTimeout(total=30)
        )
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
    
    async def get_user_id(self, username: str) -> Optional[str]:
        try:
            async with self.session.get(f"{self.base_url}/users/by/username/{username}") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("data", {}).get("id")
                logger.error(f"Failed to get user ID for {username}: {resp.status}")
                return None
        except Exception as e:
            logger.error(f"Error getting user ID for {username}: {e}")
            return None
    
    async def get_recent_tweets(self, user_id: str, since_id: Optional[str] = None) -> List[dict]:
        params = {
            "max_results": 10,
            "tweet.fields": "id,text,created_at",
            "exclude": "retweets,replies"
        }
        if since_id:
            params["since_id"] = since_id
        
        try:
            async with self.session.get(f"{self.base_url}/users/{user_id}/tweets", params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("data", [])
                elif resp.status == 429:
                    logger.warning("Rate limit hit, waiting...")
                    await asyncio.sleep(60)
                else:
                    logger.error(f"Failed to get tweets for {user_id}: {resp.status}")
                return []
        except Exception as e:
            logger.error(f"Error getting tweets for {user_id}: {e}")
            return []

# Global variables for the bot
config_manager = None
telegram_bot = None
twitter_monitor = None
monitoring_task = None
is_monitoring = False

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    welcome_text = (
        "🤖 **Twitter-Telegram Monitor Bot**\n\n"
        "Available commands:\n"
        "• `/add <username>` - Add Twitter account to monitor\n"
        "• `/remove <username>` - Remove Twitter account\n"
        "• `/list` - List monitored accounts\n"
        "• `/status` - Show bot status\n\n"
        "Example: `/add elonmusk`"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")
    logger.info(f"Start command from user: {update.effective_user.first_name}")

async def cmd_add_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /add command"""
    global config_manager, twitter_monitor
    
    if not context.args:
        await update.message.reply_text("Usage: /add <username>\nExample: /add elonmusk")
        return
    
    username = context.args[0].replace("@", "").strip()
    
    # Get user ID from Twitter
    if not twitter_monitor:
        await update.message.reply_text("❌ Twitter monitoring not available (missing bearer token)")
        return
    
    user_id = await twitter_monitor.get_user_id(username)
    if not user_id:
        await update.message.reply_text(f"❌ Could not find Twitter user @{username}")
        return
    
    # Add to config
    if config_manager.add_account(username, user_id):
        await config_manager.save_config()
        await update.message.reply_text(f"✅ Added @{username} to monitoring list")
        logger.info(f"Added @{username} to monitoring")
    else:
        await update.message.reply_text(f"⚠️ @{username} is already being monitored")

async def cmd_remove_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /remove command"""
    global config_manager
    
    if not context.args:
        await update.message.reply_text("Usage: /remove <username>")
        return
    
    username = context.args[0].replace("@", "").strip()
    
    if config_manager.remove_account(username):
        await config_manager.save_config()
        await update.message.reply_text(f"✅ Removed @{username} from monitoring list")
        logger.info(f"Removed @{username} from monitoring")
    else:
        await update.message.reply_text(f"❌ @{username} was not being monitored")

async def cmd_list_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /list command"""
    global config_manager
    
    accounts = config_manager.get_accounts()
    
    if not accounts:
        await update.message.reply_text("📝 No accounts are currently being monitored")
        return
    
    account_list = "\n".join([f"• @{acc.username}" for acc in accounts])
    message = f"📝 **Monitored Accounts ({len(accounts)}):**\n\n{account_list}"
    await update.message.reply_text(message, parse_mode="Markdown")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command"""
    global config_manager, is_monitoring, twitter_monitor
    
    accounts = config_manager.get_accounts()
    twitter_status = "✅ Connected" if twitter_monitor else "❌ No bearer token"
    
    status_text = (
        f"🤖 **Bot Status**\n\n"
        f"• Commands: ✅ Working\n"
        f"• Twitter API: {twitter_status}\n"
        f"• Monitoring: {'✅ Active' if is_monitoring else '⏸️ Inactive'}\n"
        f"• Monitored accounts: {len(accounts)}\n"
        f"• Check interval: {config_manager.config['check_interval']}s"
    )
    await update.message.reply_text(status_text, parse_mode="Markdown")

async def send_tweet_to_telegram(tweet: dict, username: str, chat_id: str):
    """Send tweet to Telegram"""
    global telegram_bot
    
    try:
        text = tweet.get("text", "")
        tweet_id = tweet.get("id", "")
        created_at = tweet.get("created_at", "")
        
        # Format timestamp
        if created_at:
            try:
                dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                time_str = dt.strftime("%Y-%m-%d %H:%M UTC")
            except:
                time_str = created_at
        else:
            time_str = "Unknown time"
        
        # Create message
        tweet_url = f"https://twitter.com/{username}/status/{tweet_id}"
        message = (
            f"🐦 **New tweet from @{username}**\n\n"
            f"{text}\n\n"
            f"📅 {time_str}\n"
            f"🔗 [View on Twitter]({tweet_url})"
        )
        
        await telegram_bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode="Markdown",
            disable_web_page_preview=False
        )
        logger.info(f"Forwarded tweet from @{username}")
        
    except Exception as e:
        logger.error(f"Failed to send tweet: {e}")

async def check_account(account: TwitterAccount, chat_id: str):
    """Check single account for new tweets"""
    global config_manager, twitter_monitor
    
    try:
        tweets = await twitter_monitor.get_recent_tweets(
            account.user_id, account.last_tweet_id
        )
        
        if tweets:
            # Process in chronological order
            tweets.sort(key=lambda x: int(x["id"]))
            
            for tweet in tweets:
                await send_tweet_to_telegram(tweet, account.username, chat_id)
                config_manager.update_last_tweet(account.username, tweet["id"])
                await asyncio.sleep(1)  # Rate limiting
                
    except Exception as e:
        logger.error(f"Error checking @{account.username}: {e}")

async def monitoring_loop(chat_id: str):
    """Main monitoring loop"""
    global config_manager, is_monitoring
    
    logger.info("Starting Twitter monitoring loop...")
    is_monitoring = True
    
    while is_monitoring:
        try:
            accounts = config_manager.get_accounts()
            
            if accounts:
                # Check all accounts
                await asyncio.gather(
                    *[check_account(account, chat_id) for account in accounts],
                    return_exceptions=True
                )
                await config_manager.save_config()
            
            await asyncio.sleep(config_manager.config["check_interval"])
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in monitoring loop: {e}")
            await asyncio.sleep(10)
    
    logger.info("Monitoring loop stopped")

async def main():
    """Main function"""
    global config_manager, telegram_bot, twitter_monitor, monitoring_task, is_monitoring
    
    print("🚀 Starting Complete Twitter-Telegram Bot...")
    
    # Initialize config manager
    config_manager = ConfigManager()
    
    # Load credentials
    try:
        if os.path.exists("credentials.json"):
            with open("credentials.json", "r") as f:
                creds = json.load(f)
            telegram_token = creds.get("TELEGRAM_BOT_TOKEN")
            twitter_bearer_token = creds.get("TWITTER_BEARER_TOKEN")
            chat_id = creds.get("TELEGRAM_CHAT_ID")
            print("✅ Loaded credentials from credentials.json")
        else:
            telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
            twitter_bearer_token = os.getenv("TWITTER_BEARER_TOKEN")
            chat_id = os.getenv("TELEGRAM_CHAT_ID")
            print("✅ Loaded credentials from environment")
        
        if not telegram_token:
            print("❌ Missing TELEGRAM_BOT_TOKEN")
            return
            
        if not twitter_bearer_token:
            print("⚠️ Missing TWITTER_BEARER_TOKEN - /add command won't work")
        
        if not chat_id:
            print("⚠️ Missing TELEGRAM_CHAT_ID - tweet forwarding won't work")
            
    except Exception as e:
        print(f"❌ Error loading credentials: {e}")
        return
    
    # Initialize Twitter monitor if token available
    if twitter_bearer_token:
        twitter_monitor = TwitterMonitor(twitter_bearer_token)
        print("✅ Twitter API initialized")
    
    # Create and start Telegram bot
    try:
        app = Application.builder().token(telegram_token).build()
        telegram_bot = app.bot
        
        # Add command handlers
        app.add_handler(CommandHandler("start", cmd_start))
        app.add_handler(CommandHandler("add", cmd_add_account))
        app.add_handler(CommandHandler("remove", cmd_remove_account))
        app.add_handler(CommandHandler("list", cmd_list_accounts))
        app.add_handler(CommandHandler("status", cmd_status))
        
        print("✅ Command handlers registered")
        
        # Start bot with polling
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        
        print("🤖 Bot is running!")
        print("📱 Try /start in Telegram to begin")
        
        # Start Twitter monitoring if we have all credentials
        if twitter_monitor and chat_id:
            async with twitter_monitor:
                monitoring_task = asyncio.create_task(monitoring_loop(chat_id))
                print("🐦 Twitter monitoring started!")
                
                try:
                    while True:
                        await asyncio.sleep(1)
                except KeyboardInterrupt:
                    print("\n👋 Stopping bot...")
                    is_monitoring = False
                    if monitoring_task:
                        monitoring_task.cancel()
        else:
            print("⚠️ Twitter monitoring disabled (missing credentials)")
            try:
                while True:
                    await asyncio.sleep(1)
            except KeyboardInterrupt:
                print("\n👋 Stopping bot...")
        
    except Exception as e:
        print(f"❌ Error starting bot: {e}")
    
    finally:
        try:
            is_monitoring = False
            if 'app' in locals():
                await app.updater.stop()
                await app.stop()
                await app.shutdown()
            print("✅ Bot stopped cleanly")
        except:
            pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Bot stopped by user")
    except Exception as e:
        print(f"❌ Fatal error: {e}")