import os
print("Starting Discord Bot from workspace...")
import sys
import traceback
import discord
from discord.ext import commands
import asyncio
import logging

# Get config from environment variables
BOT_TOKEN = os.getenv('BOT_TOKEN')
BOT_ID = os.getenv('BOT_ID', 'N/A') # Default to N/A if not provided

# Setup logging
log_format = f'[{BOT_ID}] %(asctime)s - %(levelname)s - %(message)s'
logging.basicConfig(
    level=logging.INFO,
    format=log_format,
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

logger.info("🚀 Starting Discord Bot...")
logger.info(f"Python version: {sys.version}")
logger.info(f"BOT_TOKEN value: {BOT_TOKEN}")

# Validate token
if not BOT_TOKEN:
    logger.error("❌ ERROR: BOT_TOKEN not found in environment.")
    sys.exit(1)

if len(BOT_TOKEN.strip()) < 50:  # Discord tokens are typically 59+ characters
    logger.error(f"❌ ERROR: BOT_TOKEN appears to be invalid (too short). Length: {len(BOT_TOKEN.strip())}")
    sys.exit(1)

logger.info(f"✅ Token loaded: {BOT_TOKEN[:5]}...{BOT_TOKEN[-4:]}")

# Setup Discord intents
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
logger.info("✅ Discord intents configured")

# Create bot instance
try:
    bot = commands.Bot(
        command_prefix='$',
        intents=intents,
        help_command=None  # Disable default help command
    )
    logger.info("✅ Bot instance created successfully")
except Exception as e:
    logger.error(f"❌ Failed to create bot instance: {e}")
    traceback.print_exc()
    sys.exit(1)

@bot.event
async def on_ready():
    logger.info("=" * 50)
    logger.info("🎉 DISCORD BOT IS NOW ONLINE!")
    logger.info(f"Bot Name: {bot.user.name}")
    logger.info(f"Bot ID: {bot.user.id}")
    logger.info(f"Discord.py Version: {discord.__version__}")
    logger.info(f"Connected to {len(bot.guilds)} server(s)")
    logger.info("=" * 50)

    if bot.guilds:
        logger.info("📋 Connected servers:")
        for guild in bot.guilds:
            logger.info(f"  - {guild.name} ({guild.id}) - {guild.member_count} members")
    else:
        logger.warning("⚠️ Bot is not connected to any servers!")
        logger.warning("Please invite your bot to a server using the Discord Developer Portal")

    logger.info("✅ Bot is ready to receive commands!")

@bot.event
async def on_connect():
    logger.info("🔗 Bot connected to Discord WebSocket")

@bot.event
async def on_disconnect():
    logger.warning("🔌 Bot disconnected from Discord WebSocket")

@bot.event
async def on_resumed():
    logger.info("🔄 Bot connection resumed")

@bot.event
async def on_error(event, *args, **kwargs):
    logger.error(f"❌ Error in event '{event}':")
    traceback.print_exc()

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return  # Ignore command not found errors
    logger.error(f"❌ Command error in {ctx.command}: {error}")
    await ctx.send(f"❌ Error: {error}")

# Bot Commands
@bot.command(name='hello', help='Say hello to the bot')
async def hello_command(ctx):
    await ctx.send(f'Hello {ctx.author.mention}! 👋 I am online and working!')
    logger.info(f"✅ Hello command executed by {ctx.author} in {ctx.guild.name if ctx.guild else 'DM'}")

@bot.command(name='ping', help='Check bot latency')
async def ping_command(ctx):
    latency = round(bot.latency * 1000)
    await ctx.send(f'🏓 Pong! Latency: {latency}ms')
    logger.info(f"✅ Ping command executed: {latency}ms latency")

@bot.command(name='test', help='Run a comprehensive bot test')
async def test_command(ctx):
    embed = discord.Embed(
        title="🤖 Bot Status Test",
        description="All systems operational!",
        color=0x00ff00,
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="✅ Connection", value="Stable", inline=True)
    embed.add_field(name="✅ Commands", value="Working", inline=True)
    embed.add_field(name="✅ Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.set_footer(text=f"Requested by {ctx.author.name}")

    await ctx.send(embed=embed)
    logger.info(f"✅ Test command executed by {ctx.author} in {ctx.guild.name if ctx.guild else 'DM'}")

@bot.command(name='info', help='Get bot information')
async def info_command(ctx):
    embed = discord.Embed(
        title="🤖 Bot Information",
        color=0x3498db,
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="Bot Name", value=bot.user.name, inline=True)
    embed.add_field(name="Bot ID", value=bot.user.id, inline=True)
    embed.add_field(name="Servers", value=len(bot.guilds), inline=True)
    embed.add_field(name="Python Version", value=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}", inline=True)
    embed.add_field(name="Discord.py Version", value=discord.__version__, inline=True)
    embed.add_field(name="Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)

    await ctx.send(embed=embed)

# Error handling for the bot startup
async def main():
    try:
        logger.info("🔄 Attempting to start bot...")
        await bot.start(BOT_TOKEN)
    except discord.LoginFailure:
        logger.error("❌ LOGIN FAILED: Invalid bot token!")
        logger.error("Please check your bot token in the Discord Developer Portal")
        sys.exit(1)
    except discord.HTTPException as e:
        logger.error(f"❌ HTTP Error occurred: {e}")
        logger.error("This might be a temporary Discord API issue")
        sys.exit(1)
    except discord.ConnectionClosed as e:
        logger.error(f"❌ Connection closed: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"❌ Unexpected error during startup: {e}")
        traceback.print_exc()
        sys.exit(1)

# Run the bot
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Bot stopped by user")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")
        traceback.print_exc()
        sys.exit(1)
