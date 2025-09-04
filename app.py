import os
import requests
from flask import Flask, render_template, redirect, url_for, request
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
from dotenv import load_dotenv
from pymongo import MongoClient
from bson.objectid import ObjectId
import datetime
from flask_socketio import SocketIO, emit, join_room
import time
import threading
import subprocess
import shlex
import signal
import sys
import json
import select
import logging

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY")
socketio = SocketIO(app, cors_allowed_origins="*")

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_WORKSPACES_PATH = "bot_workspaces"
if not os.path.exists(BOT_WORKSPACES_PATH):
    os.makedirs(BOT_WORKSPACES_PATH)

# Enhanced bot process management
running_bots = {} # {bot_id: {"process": subprocess.Popen, "thread": threading.Thread, "status": "running"}}

# Discord OAuth2 settings
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "http://localhost:5000/callback")
DISCORD_API_BASE_URL = "https://discord.com/api"
DISCORD_AUTHORIZATION_URL = f"https://discord.com/api/oauth2/authorize?client_id={DISCORD_CLIENT_ID}&redirect_uri={DISCORD_REDIRECT_URI}&response_type=code&scope=identify%20email"

# MongoDB setup
mongo_client = MongoClient(os.getenv("MONGO_URI"))
db = mongo_client['dash-bot']

# Flask-Login setup
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

class User(UserMixin):
    def __init__(self, id, username, email, avatar_url):
        self.id = id
        self.username = username
        self.email = email
        self.avatar_url = avatar_url

    @staticmethod
    def get(user_id):
        user_data = db.users.find_one({"_id": ObjectId(user_id)})
        if user_data:
            return User(
                id=user_data["_id"],
                username=user_data["username"],
                email=user_data["email"],
                avatar_url=user_data.get("avatar_url")
            )
        return None

    @staticmethod
    def create_or_update(discord_id, username, email, avatar_url):
        user_data = {
            "discord_id": discord_id,
            "username": username,
            "avatar_url": avatar_url,
        }
        db.users.update_one(
            {"email": email},
            {
                "$set": user_data,
                "$setOnInsert": {"bots": [], "email": email}
            },
            upsert=True
        )
        user_doc = db.users.find_one({"email": email})
        return User(
            id=user_doc["_id"],
            username=user_doc["username"],
            email=user_doc["email"],
            avatar_url=user_doc.get("avatar_url")
        )

@login_manager.user_loader
def load_user(user_id):
    return User.get(user_id)

def cleanup_bot_processes():
    """Clean up all running bot processes on shutdown"""
    print("Cleaning up bot processes...")
    for bot_id, bot_info in running_bots.items():
        try:
            process = bot_info["process"]
            if process.poll() is None:  # Process is still running
                print(f"Terminating bot {bot_id}...")
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    print(f"Killing bot {bot_id}...")
                    process.kill()
        except Exception as e:
            print(f"Error cleaning up bot {bot_id}: {e}")

def signal_handler(sig, frame):
    """Handle shutdown signals"""
    print("Received shutdown signal...")
    cleanup_bot_processes()
    sys.exit(0)

# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/login")
def login():
    return redirect(DISCORD_AUTHORIZATION_URL)

@app.route("/callback")
def callback():
    code = request.args.get("code")
    data = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "scope": "identify email"
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded"
    }
    response = requests.post(f"{DISCORD_API_BASE_URL}/oauth2/token", data=data, headers=headers)
    token_data = response.json()
    access_token = token_data.get("access_token")

    if not access_token:
        print(f"Error getting access token from Discord: {token_data}")
        return "Failed to retrieve access token.", 400

    headers = {
        "Authorization": f"Bearer {access_token}"
    }
    user_response = requests.get(f"{DISCORD_API_BASE_URL}/users/@me", headers=headers)
    user_data = user_response.json()

    discord_id = user_data["id"]
    email = user_data["email"]
    username = user_data["username"]
    avatar_hash = user_data.get("avatar")
    avatar_url = f"https://cdn.discordapp.com/avatars/{discord_id}/{avatar_hash}.png" if avatar_hash else None

    user = User.create_or_update(
        discord_id=discord_id,
        username=username,
        email=email,
        avatar_url=avatar_url
    )

    login_user(user)

    return redirect(url_for("dashboard"))

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("index"))

@app.route("/dashboard")
@login_required
def dashboard():
    user_data = db.users.find_one({"_id": current_user.id})
    servers = user_data.get("servers", [])

    for i, server in enumerate(servers):
        bot_id = f"{current_user.id}_{server.get('server_name', i)}"
        bot_dir = os.path.join(BOT_WORKSPACES_PATH, bot_id)

        if not os.path.exists(bot_dir):
            os.makedirs(bot_dir)

            # Create .env file
            with open(os.path.join(bot_dir, ".env"), "w") as f:
                f.write(f"BOT_TOKEN={server.get('botToken', '')}")

            # Auto-generate ENHANCED main.py with better error handling
            main_py_content = f"""import os
import sys
import traceback
from dotenv import load_dotenv
import discord
from discord.ext import commands
import asyncio
import logging

# Setup logging for better debugging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

print("🚀 Starting Discord Bot...")
print(f"Python version: {{sys.version}}")

# Load environment variables
load_dotenv()
print("✅ Environment loaded")

# Validate token
token = os.getenv('BOT_TOKEN')
if not token:
    print("❌ ERROR: BOT_TOKEN not found in .env file")
    print("Please check your .env file contains: BOT_TOKEN=your_bot_token_here")
    sys.exit(1)

if len(token.strip()) < 50:  # Discord tokens are typically 59+ characters
    print("❌ ERROR: BOT_TOKEN appears to be invalid (too short)")
    print(f"Token length: {{len(token.strip())}} characters")
    sys.exit(1)

print(f"✅ Token loaded: {{token[:10]}}...{{token[-4:]}}")

# Test network connectivity
try:
    import requests
    print("🌐 Testing Discord API connectivity...")
    response = requests.get("https://discord.com/api/v10/gateway", timeout=10)
    if response.status_code == 200:
        print("✅ Discord API is reachable")
    else:
        print(f"⚠️ Discord API returned status: {{response.status_code}}")
except Exception as e:
    print(f"❌ Network connectivity test failed: {{e}}")

# Setup Discord intents
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

print("✅ Discord intents configured")

# Create bot instance
try:
    bot = commands.Bot(
        command_prefix='$', 
        intents=intents,
        help_command=None  # Disable default help command
    )
    print("✅ Bot instance created successfully")
except Exception as e:
    print(f"❌ Failed to create bot instance: {{e}}")
    traceback.print_exc()
    sys.exit(1)

@bot.event
async def on_ready():
    print("=" * 50)
    print("🎉 DISCORD BOT IS NOW ONLINE!")
    print(f"Bot Name: {{bot.user.name}}")
    print(f"Bot ID: {{bot.user.id}}")
    print(f"Discord.py Version: {{discord.__version__}}")
    print(f"Connected to {{len(bot.guilds)}} server(s)")
    print("=" * 50)
    
    # List all servers the bot is in
    if bot.guilds:
        print("📋 Connected servers:")
        for guild in bot.guilds:
            print(f"  - {{guild.name}} ({{guild.id}}) - {{guild.member_count}} members")
    else:
        print("⚠️ Bot is not connected to any servers!")
        print("Please invite your bot to a server using the Discord Developer Portal")
    
    print("✅ Bot is ready to receive commands!")

@bot.event
async def on_connect():
    print("🔗 Bot connected to Discord WebSocket")

@bot.event
async def on_disconnect():
    print("🔌 Bot disconnected from Discord WebSocket")

@bot.event
async def on_resumed():
    print("🔄 Bot connection resumed")

@bot.event
async def on_error(event, *args, **kwargs):
    print(f"❌ Error in event '{{event}}':")
    traceback.print_exc()

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return  # Ignore command not found errors
    print(f"❌ Command error in {{ctx.command}}: {{error}}")
    await ctx.send(f"❌ Error: {{error}}")

# Bot Commands
@bot.command(name='hello', help='Say hello to the bot')
async def hello_command(ctx):
    await ctx.send(f'Hello {{ctx.author.mention}}! 👋 I am online and working!')
    print(f"✅ Hello command executed by {{ctx.author}} in {{ctx.guild.name if ctx.guild else 'DM'}}")

@bot.command(name='ping', help='Check bot latency')
async def ping_command(ctx):
    latency = round(bot.latency * 1000)
    await ctx.send(f'🏓 Pong! Latency: {{latency}}ms')
    print(f"✅ Ping command executed: {{latency}}ms latency")

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
    embed.add_field(name="✅ Latency", value=f"{{round(bot.latency * 1000)}}ms", inline=True)
    embed.set_footer(text=f"Requested by {{ctx.author.name}}")
    
    await ctx.send(embed=embed)
    print(f"✅ Test command executed by {{ctx.author}} in {{ctx.guild.name if ctx.guild else 'DM'}}")

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
    embed.add_field(name="Python Version", value=f"{{sys.version_info.major}}.{{sys.version_info.minor}}.{{sys.version_info.micro}}", inline=True)
    embed.add_field(name="Discord.py Version", value=discord.__version__, inline=True)
    embed.add_field(name="Latency", value=f"{{round(bot.latency * 1000)}}ms", inline=True)
    
    await ctx.send(embed=embed)

# Error handling for the bot startup
async def main():
    try:
        print("🔄 Attempting to start bot...")
        await bot.start(token)
    except discord.LoginFailure:
        print("❌ LOGIN FAILED: Invalid bot token!")
        print("Please check your bot token in the Discord Developer Portal")
        sys.exit(1)
    except discord.HTTPException as e:
        print(f"❌ HTTP Error occurred: {{e}}")
        print("This might be a temporary Discord API issue")
        sys.exit(1)
    except discord.ConnectionClosed as e:
        print(f"❌ Connection closed: {{e}}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Unexpected error during startup: {{e}}")
        traceback.print_exc()
        sys.exit(1)

# Run the bot
if __name__ == "__main__":
    try:
        # Use asyncio.run for proper async handling
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Bot stopped by user")
    except Exception as e:
        print(f"❌ Fatal error: {{e}}")
        traceback.print_exc()
        sys.exit(1)
"""
            with open(os.path.join(bot_dir, "main.py"), "w") as f:
                f.write(main_py_content)

            # Create requirements.txt
            requirements_content = """discord.py>=2.3.0
python-dotenv>=1.0.0
aiohttp>=3.8.0
"""
            with open(os.path.join(bot_dir, "requirements.txt"), "w") as f:
                f.write(requirements_content)

            # Set default startup command in the database
            db.users.update_one(
                {"_id": current_user.id},
                {"$set": {f"servers.{i}.startup_command": "python -u main.py"}}
            )
            # Add files array to the server object
            db.users.update_one(
                {"_id": current_user.id},
                {"$set": {f"servers.{i}.files": [
                    {"path": "main.py", "type": "file", "content": main_py_content}, 
                    {"path": ".env", "type": "file", "content": f"BOT_TOKEN={server.get('botToken', '')}"},
                    {"path": "requirements.txt", "type": "file", "content": requirements_content}
                ]}}
            )

        # Update server status based on running processes
        if bot_id in running_bots:
            process = running_bots[bot_id]["process"]
            if process.poll() is None:
                server_status = "online"
            else:
                server_status = "offline"
                # Clean up dead process
                del running_bots[bot_id]
        else:
            server_status = "offline"
        
        # Update status in database
        db.users.update_one(
            {"_id": current_user.id},
            {"$set": {f"servers.{i}.status": server_status}}
        )

    # Re-fetch user_data to get the updated server list
    user_data = db.users.find_one({"_id": current_user.id})
    servers = user_data.get("servers", [])

    return render_template("dashboard.html", user=current_user, servers=servers)

from flask import jsonify

def build_file_tree(file_list):
    tree = {}
    for file_doc in sorted(file_list, key=lambda x: x['path']):
        path_parts = file_doc['path'].split('/')
        current_level = tree
        for part in path_parts[:-1]:
            current_level = current_level.setdefault(part, {})

        if file_doc['type'] == 'file':
            current_level[path_parts[-1]] = file_doc.get('content', '')
        else:
            current_level[path_parts[-1]] = {}

    return tree

@app.route("/editor/<int:server_index>")
@login_required
def editor(server_index):
    user_data = db.users.find_one({"_id": current_user.id})
    servers = user_data.get("servers", [])
    if server_index >= len(servers):
        return "Not Found", 404

    bot_data = servers[server_index]
    bot_id = f"{current_user.id}_{bot_data.get('server_name', server_index)}"

    # Check current bot status
    if bot_id in running_bots:
        process = running_bots[bot_id]["process"]
        if process.poll() is None:
            bot_data['status'] = "online"
        else:
            bot_data['status'] = "offline"
            del running_bots[bot_id]
    else:
        bot_data['status'] = "offline"

    file_tree = build_file_tree(bot_data.get('files', []))
    bot_data['files_tree'] = file_tree
    bot_data['_id'] = bot_id

    return render_template("editor.html", bot=bot_data, user=current_user)

# Debug endpoint
@app.route("/api/server/<int:server_index>/debug", methods=["GET"])
@login_required  
def debug_bot(server_index):
    user_data = db.users.find_one({"_id": current_user.id})
    servers = user_data.get("servers", [])
    
    if server_index >= len(servers):
        return jsonify({"error": "Server not found"}), 404

    bot_data = servers[server_index]
    bot_id = f"{current_user.id}_{bot_data.get('server_name', server_index)}"
    bot_dir = os.path.join(BOT_WORKSPACES_PATH, bot_id)
    
    # Check .env file
    env_file = os.path.join(bot_dir, ".env")
    debug_info = {"bot_dir": bot_dir, "bot_id": bot_id}
    
    if os.path.exists(env_file):
        try:
            with open(env_file, 'r') as f:
                env_content = f.read()
            
            has_token = "BOT_TOKEN=" in env_content
            if has_token:
                token_line = [line for line in env_content.split('\n') if line.startswith('BOT_TOKEN=')]
                if token_line:
                    token = token_line[0].split('=', 1)[1].strip()
                    debug_info.update({
                        "env_exists": True,
                        "has_token": True,
                        "token_length": len(token),
                        "token_preview": f"{token[:10]}...{token[-4:]}" if len(token) > 14 else "too_short",
                        "token_valid_length": len(token) >= 50
                    })
                else:
                    debug_info.update({
                        "env_exists": True,
                        "has_token": False,
                        "error": "BOT_TOKEN line not found"
                    })
            else:
                debug_info.update({
                    "env_exists": True,
                    "has_token": False,
                    "error": "BOT_TOKEN not in file"
                })
        except Exception as e:
            debug_info.update({
                "env_exists": True,
                "error": f"Failed to read .env: {str(e)}"
            })
    else:
        debug_info.update({"env_exists": False})
    
    # Test Discord API connectivity
    try:
        import requests
        response = requests.get("https://discord.com/api/v10/gateway", timeout=10)
        debug_info["discord_api"] = {
            "reachable": response.status_code == 200,
            "status_code": response.status_code
        }
    except Exception as e:
        debug_info["discord_api"] = {
            "reachable": False,
            "error": str(e)
        }
    
    return jsonify(debug_info)

@app.route("/api/server/<int:server_index>/test-discord", methods=["POST"])
@login_required
def test_discord_connection(server_index):
    try:
        # Test Discord API connectivity
        response = requests.get("https://discord.com/api/v10/gateway", timeout=10)
        if response.status_code == 200:
            return jsonify({"discord_api": "reachable", "status": "ok", "gateway": response.json()})
        else:
            return jsonify({"discord_api": "unreachable", "status": "error", "code": response.status_code})
    except Exception as e:
        return jsonify({"discord_api": "error", "status": "error", "error": str(e)})

@app.route("/api/server/<int:server_index>/file", methods=["GET", "POST"])
@login_required
def file_content(server_index):
    user_data = db.users.find_one({"_id": current_user.id})
    servers = user_data.get("servers", [])
    if server_index >= len(servers):
        return jsonify({"error": "Server not found"}), 404

    bot_data = servers[server_index]
    bot_id = f"{current_user.id}_{bot_data.get('server_name', server_index)}"
    bot_dir = os.path.join(BOT_WORKSPACES_PATH, bot_id)

    path = request.args.get("path")
    if not path:
        return jsonify({"error": "File path is required"}), 400

    full_path = os.path.join(bot_dir, path)

    if request.method == "GET":
        try:
            with open(full_path, "r", encoding='utf-8') as f:
                content = f.read()
            return jsonify({"content": content})
        except FileNotFoundError:
            return jsonify({"error": "File not found"}), 404
        except Exception as e:
            return jsonify({"error": f"Error reading file: {str(e)}"}), 500

    if request.method == "POST":
        try:
            content = request.json.get("content", "")
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding='utf-8') as f:
                f.write(content)
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"error": f"Error saving file: {str(e)}"}), 500

@app.route("/api/server/<int:server_index>/files/create", methods=["POST"])
@login_required
def create_file(server_index):
    path = request.json.get("path")
    file_type = request.json.get("type")
    if not path or not file_type:
        return jsonify({"error": "Path and type are required"}), 400

    user_data = db.users.find_one({"_id": current_user.id})
    servers = user_data.get("servers", [])
    if server_index >= len(servers):
        return jsonify({"error": "Server not found"}), 404

    bot_data = servers[server_index]
    bot_id = f"{current_user.id}_{bot_data.get('server_name', server_index)}"
    bot_dir = os.path.join(BOT_WORKSPACES_PATH, bot_id)
    full_path = os.path.join(bot_dir, path)

    if os.path.exists(full_path):
        return jsonify({"error": "File or folder with this path already exists"}), 400

    try:
        if file_type == "folder":
            os.makedirs(full_path, exist_ok=True)
        else:
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding='utf-8') as f:
                f.write("")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": f"Error creating file/folder: {str(e)}"}), 500

@app.route("/api/server/<int:server_index>/files/delete", methods=["POST"])
@login_required
def delete_file(server_index):
    path = request.json.get("path")
    if not path:
        return jsonify({"error": "Path is required"}), 400

    user_data = db.users.find_one({"_id": current_user.id})
    servers = user_data.get("servers", [])
    if server_index >= len(servers):
        return jsonify({"error": "Server not found"}), 404

    bot_data = servers[server_index]
    bot_id = f"{current_user.id}_{bot_data.get('server_name', server_index)}"
    bot_dir = os.path.join(BOT_WORKSPACES_PATH, bot_id)
    full_path = os.path.join(bot_dir, path)

    try:
        if os.path.isdir(full_path):
            import shutil
            shutil.rmtree(full_path)
        elif os.path.isfile(full_path):
            os.remove(full_path)
        else:
            return jsonify({"error": "File or folder not found"}), 404
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": f"Error deleting file/folder: {str(e)}"}), 500

def stream_bot_logs(bot_id, process):
    """Enhanced log streaming with proper process management using select"""
    try:
        socketio.emit('log', {
            'data': f'[{datetime.datetime.now().strftime("%H:%M:%S")}] 🚀 Bot process started (PID: {process.pid})'
        }, room=bot_id)

        # Use select for non-blocking reads on stdout and stderr
        streams = [process.stdout, process.stderr]
        while process.poll() is None:
            readable, _, _ = select.select(streams, [], [], 0.1)
            for stream in readable:
                line = stream.readline().strip()
                if line:
                    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
                    log_level = '[ERROR]' if stream is process.stderr else ''
                    socketio.emit('log', {
                        'data': f'[{timestamp}] {log_level} {line}'.strip()
                    }, room=bot_id)

        # After process finishes, read any remaining output
        for stream in streams:
            for line in stream.readlines():
                line = line.strip()
                if line:
                    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
                    log_level = '[ERROR]' if stream is process.stderr else ''
                    socketio.emit('log', {
                        'data': f'[{timestamp}] {log_level} {line}'.strip()
                    }, room=bot_id)

        # Process finished
        return_code = process.returncode
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        
        if return_code == 0:
            socketio.emit('log', {
                'data': f'[{timestamp}] ✅ Bot process finished successfully'
            }, room=bot_id)
        else:
            socketio.emit('log', {
                'data': f'[{timestamp}] ⚠️ Bot process exited with code {return_code}'
            }, room=bot_id)
            
    except Exception as e:
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        socketio.emit('log', {
            'data': f'[{timestamp}] 💥 Log streaming error: {str(e)}'
        }, room=bot_id)
    finally:
        # Clean up
        if bot_id in running_bots and running_bots[bot_id]["process"].pid == process.pid:
            running_bots[bot_id]["status"] = "stopped"

@socketio.on('connect', namespace='/editor')
def editor_connect():
    emit('log', {'data': '🔌 Connected to console...'})

@socketio.on('join', namespace='/editor')
def on_join(data):
    bot_id = data['bot_id']
    join_room(bot_id)
    
    # Check if bot is running and send status
    if bot_id in running_bots:
        process = running_bots[bot_id]["process"]
        if process.poll() is None:
            emit('log', {'data': f'📡 Console connected - Bot {bot_id} is running (PID: {process.pid})'}, room=bot_id)
        else:
            emit('log', {'data': f'📡 Console connected - Bot {bot_id} is stopped'}, room=bot_id)
            # Clean up dead process
            del running_bots[bot_id]
    else:
        emit('log', {'data': f'📡 Console connected - Bot {bot_id} is offline'}, room=bot_id)

@app.route("/api/server/<int:server_index>/start", methods=["POST"])
@login_required
def start_bot(server_index):
    user_data = db.users.find_one({"_id": current_user.id})
    servers = user_data.get("servers", [])
    
    if server_index >= len(servers):
        return jsonify({"error": "Server not found"}), 404

    bot_data = servers[server_index]
    bot_id = f"{current_user.id}_{bot_data.get('server_name', server_index)}"

    # Check if bot is already running
    if bot_id in running_bots:
        process = running_bots[bot_id]["process"]
        if process.poll() is None:  # Still running
            socketio.emit('log', {
                'data': f'⚠️ Bot {bot_id} is already running (PID: {process.pid})'
            }, room=bot_id)
            return jsonify({"error": "Bot is already running"}), 400
        else:
            # Process died, clean up
            del running_bots[bot_id]

    bot_dir = os.path.join(BOT_WORKSPACES_PATH, bot_id)
    startup_command = bot_data.get("startup_command", "python -u main.py")

    # Enhanced startup sequence with better validation
    socketio.emit('log', {
        'data': f'🔄 Starting bot {bot_id}...'
    }, room=bot_id)

    # Pre-flight checks
    if not os.path.isdir(bot_dir):
        error_msg = f'❌ Workspace directory not found: {bot_dir}'
        socketio.emit('log', {'data': error_msg}, room=bot_id)
        return jsonify({"error": "Workspace not found"}), 404

    # Check .env file and validate token
    env_file = os.path.join(bot_dir, ".env")
    if not os.path.exists(env_file):
        error_msg = f'❌ .env file not found in {bot_dir}'
        socketio.emit('log', {'data': error_msg}, room=bot_id)
        return jsonify({"error": ".env file not found"}), 404

    # Validate token in .env
    try:
        with open(env_file, 'r') as f:
            env_content = f.read()
        
        if "BOT_TOKEN=" not in env_content:
            error_msg = "❌ BOT_TOKEN not found in .env file"
            socketio.emit('log', {'data': error_msg}, room=bot_id)
            return jsonify({"error": "BOT_TOKEN not configured"}), 400
            
        token_line = [line for line in env_content.split('\n') if line.startswith('BOT_TOKEN=')]
        if not token_line:
            error_msg = "❌ BOT_TOKEN line not found in .env file"
            socketio.emit('log', {'data': error_msg}, room=bot_id)
            return jsonify({"error": "BOT_TOKEN not configured"}), 400
            
        token = token_line[0].split('=', 1)[1].strip()
        if len(token) < 50:  # Discord tokens are typically 59+ characters
            error_msg = f"❌ BOT_TOKEN appears to be invalid (length: {len(token)} chars, expected 50+)"
            socketio.emit('log', {'data': error_msg}, room=bot_id)
            return jsonify({"error": "Invalid BOT_TOKEN"}), 400
            
        socketio.emit('log', {
            'data': f'✅ Token validation passed: {token[:10]}...{token[-4:]}'
        }, room=bot_id)
        
    except Exception as e:
        error_msg = f'❌ Error reading .env file: {str(e)}'
        socketio.emit('log', {'data': error_msg}, room=bot_id)
        return jsonify({"error": "Error reading .env file"}), 500

    # Parse and validate command
    try:
        command_parts = shlex.split(startup_command)
        if not command_parts:
            raise ValueError("Empty command")
    except Exception as e:
        error_msg = f'❌ Invalid startup command: {e}'
        socketio.emit('log', {'data': error_msg}, room=bot_id)
        return jsonify({"error": "Invalid startup command"}), 400

    # Check if main file exists
    if len(command_parts) > 1:
        main_file = command_parts[1]
        if not os.path.isfile(os.path.join(bot_dir, main_file)):
            error_msg = f'❌ Main file not found: {main_file}'
            socketio.emit('log', {'data': error_msg}, room=bot_id)
            return jsonify({"error": "Main file not found"}), 404

    # Test Discord API connectivity
    socketio.emit('log', {'data': '🌐 Testing Discord API connectivity...'}, room=bot_id)
    try:
        response = requests.get("https://discord.com/api/v10/gateway", timeout=10)
        if response.status_code != 200:
            error_msg = f'❌ Discord API unreachable (status: {response.status_code})'
            socketio.emit('log', {'data': error_msg}, room=bot_id)
            return jsonify({"error": "Discord API unreachable"}), 503
        socketio.emit('log', {'data': '✅ Discord API is reachable'}, room=bot_id)
    except Exception as e:
        error_msg = f'❌ Network connectivity test failed: {str(e)}'
        socketio.emit('log', {'data': error_msg}, room=bot_id)
        return jsonify({"error": f"Network error: {str(e)}"}), 503

    # Install dependencies from requirements.txt
    socketio.emit('log', {'data': '📦 Installing dependencies...'}, room=bot_id)
    requirements_file = os.path.join(bot_dir, "requirements.txt")
    if os.path.exists(requirements_file):
        try:
            # Using sys.executable to ensure we use the same python env
            install_command = [sys.executable, "-m", "pip", "install", "-r", requirements_file]
            install_process = subprocess.run(
                install_command,
                cwd=bot_dir,
                capture_output=True,
                text=True,
                encoding='utf-8',
                timeout=300 # 5 minute timeout for installation
            )

            # Log stdout of pip install
            if install_process.stdout:
                for line in install_process.stdout.splitlines():
                    socketio.emit('log', {'data': f'[pip] {line}'}, room=bot_id)

            if install_process.returncode == 0:
                socketio.emit('log', {'data': '✅ Dependencies installed successfully.'}, room=bot_id)
            else:
                # Log stderr of pip install
                if install_process.stderr:
                    for line in install_process.stderr.splitlines():
                        socketio.emit('log', {'data': f'[pip-error] {line}'}, room=bot_id)
                error_msg = '❌ Failed to install dependencies. Please check logs.'
                socketio.emit('log', {'data': error_msg}, room=bot_id)
                return jsonify({"error": "Failed to install dependencies"}), 500
        except subprocess.TimeoutExpired:
            error_msg = '❌ Dependency installation timed out after 5 minutes.'
            socketio.emit('log', {'data': error_msg}, room=bot_id)
            return jsonify({"error": "Dependency installation timed out"}), 500
        except Exception as e:
            error_msg = f'❌ An unexpected error occurred during dependency installation: {str(e)}'
            socketio.emit('log', {'data': error_msg}, room=bot_id)
            return jsonify({"error": "Dependency installation failed"}), 500
    else:
        socketio.emit('log', {'data': '⚠️ requirements.txt not found, skipping dependency installation.'}, room=bot_id)

    socketio.emit('log', {
        'data': f'✅ Pre-flight checks passed'
    }, room=bot_id)
    
    socketio.emit('log', {
        'data': f'🚀 Executing: {startup_command}'
    }, room=bot_id)

    try:
        # Create subprocess with proper environment
        env = dict(os.environ)
        env.update({
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8"
        })

        # Create subprocess with proper settings for real-time output
        process = subprocess.Popen(
            command_parts,
            cwd=bot_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # Line buffered
            universal_newlines=True,
            env=env
        )

        # Start log streaming thread
        log_thread = threading.Thread(
            target=stream_bot_logs,
            args=(bot_id, process),
            daemon=True
        )
        log_thread.start()

        # Store bot info
        running_bots[bot_id] = {
            "process": process,
            "thread": log_thread,
            "status": "running",
            "started_at": datetime.datetime.now(),
            "command": startup_command
        }

        socketio.emit('log', {
            'data': f'✅ Bot subprocess started (PID: {process.pid})'
        }, room=bot_id)

        return jsonify({
            "success": True, 
            "pid": process.pid,
            "command": startup_command
        })

    except Exception as e:
        error_msg = f'💥 Failed to start bot subprocess: {str(e)}'
        socketio.emit('log', {'data': error_msg}, room=bot_id)
        return jsonify({"error": f"Failed to start bot subprocess: {str(e)}"}), 500

@app.route("/api/server/<int:server_index>/stop", methods=["POST"])
@login_required
def stop_bot(server_index):
    user_data = db.users.find_one({"_id": current_user.id})
    servers = user_data.get("servers", [])
    
    if server_index >= len(servers):
        return jsonify({"error": "Server not found"}), 404

    bot_data = servers[server_index]
    bot_id = f"{current_user.id}_{bot_data.get('server_name', server_index)}"

    if bot_id not in running_bots:
        socketio.emit('log', {
            'data': f'⚠️ Bot {bot_id} is not running'
        }, room=bot_id)
        return jsonify({"error": "Bot is not running"}), 400

    bot_info = running_bots[bot_id]
    process = bot_info["process"]

    socketio.emit('log', {
        'data': f'🛑 Stopping bot {bot_id} (PID: {process.pid})...'
    }, room=bot_id)

    try:
        # Graceful termination
        process.terminate()
        
        try:
            # Wait for graceful shutdown
            process.wait(timeout=10)
            socketio.emit('log', {
                'data': f'✅ Bot stopped gracefully'
            }, room=bot_id)
        except subprocess.TimeoutExpired:
            # Force kill if it doesn't stop gracefully
            socketio.emit('log', {
                'data': f'⚠️ Bot didn\'t stop gracefully, forcing termination...'
            }, room=bot_id)
            process.kill()
            process.wait()
            socketio.emit('log', {
                'data': f'💀 Bot terminated forcefully'
            }, room=bot_id)

        # Clean up
        del running_bots[bot_id]
        
        return jsonify({"success": True})

    except Exception as e:
        error_msg = f'💥 Error stopping bot: {str(e)}'
        socketio.emit('log', {'data': error_msg}, room=bot_id)
        return jsonify({"error": f"Error stopping bot: {str(e)}"}), 500

@app.route("/api/server/<int:server_index>/status", methods=["GET"])
@login_required
def bot_status(server_index):
    user_data = db.users.find_one({"_id": current_user.id})
    servers = user_data.get("servers", [])
    
    if server_index >= len(servers):
        return jsonify({"error": "Server not found"}), 404

    bot_data = servers[server_index]
    bot_id = f"{current_user.id}_{bot_data.get('server_name', server_index)}"

    if bot_id in running_bots:
        process = running_bots[bot_id]["process"]
        if process.poll() is None:
            return jsonify({
                "status": "running",
                "pid": process.pid,
                "started_at": running_bots[bot_id]["started_at"].isoformat(),
                "command": running_bots[bot_id]["command"]
            })
        else:
            # Process died, clean up
            del running_bots[bot_id]

    return jsonify({"status": "stopped"})

@app.route("/api/server/<int:server_index>/packages/install", methods=["POST"])
@login_required
def install_package(server_index):
    package_name = request.json.get("package_name")
    if not package_name:
        return jsonify({"error": "Package name is required"}), 400

    user_data = db.users.find_one({"_id": current_user.id})
    servers = user_data.get("servers", [])
    
    if server_index >= len(servers):
        return jsonify({"error": "Server not found"}), 404

    bot_data = servers[server_index]
    bot_id = f"{current_user.id}_{bot_data.get('server_name', server_index)}"
    bot_dir = os.path.join(BOT_WORKSPACES_PATH, bot_id)

    try:
        # Install package in bot's directory context
        result = subprocess.run(
            ["pip", "install", package_name], 
            capture_output=True, 
            text=True,
            cwd=bot_dir,
            timeout=300  # 5 minute timeout
        )
        
        if result.returncode != 0:
            return jsonify({"error": f"Failed to install package: {result.stderr}"}), 500

        return jsonify({"success": True, "message": result.stdout})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Package installation timed out"}), 500
    except Exception as e:
        return jsonify({"error": f"Error installing package: {str(e)}"}), 500

@app.route("/api/server/<int:server_index>/packages/uninstall", methods=["POST"])
@login_required
def uninstall_package(server_index):
    package_name = request.json.get("package_name")
    if not package_name:
        return jsonify({"error": "Package name is required"}), 400

    try:
        result = subprocess.run(
            ["pip", "uninstall", "-y", package_name], 
            capture_output=True, 
            text=True,
            timeout=300
        )
        
        if result.returncode != 0:
            return jsonify({"error": f"Failed to uninstall package: {result.stderr}"}), 500

        return jsonify({"success": True, "message": result.stdout})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Package uninstallation timed out"}), 500
    except Exception as e:
        return jsonify({"error": f"Error uninstalling package: {str(e)}"}), 500

@app.route("/api/server/<int:server_index>/startup", methods=["POST"])
@login_required
def update_startup_command(server_index):
    command = request.json.get("startup_command")
    if command is None:
        return jsonify({"error": "Startup command is required"}), 400

    try:
        # Validate command syntax
        shlex.split(command)
    except ValueError as e:
        return jsonify({"error": f"Invalid command syntax: {str(e)}"}), 400

    db.users.update_one(
        {"_id": current_user.id},
        {"$set": {f"servers.{server_index}.startup_command": command}}
    )
    return jsonify({"success": True})

if __name__ == "__main__":
    try:
        print("🚀 Starting Flask application...")
        print(f"BOT_WORKSPACES_PATH: {BOT_WORKSPACES_PATH}")
        socketio.run(app, host='0.0.0.0', port=30158, debug=True, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        print("\nShutting down...")
        cleanup_bot_processes()
