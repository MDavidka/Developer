import os
import requests
from flask import Flask, render_template, redirect, url_for, request, jsonify
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
from dotenv import load_dotenv
from pymongo import MongoClient
from bson.objectid import ObjectId
import datetime
from flask_socketio import SocketIO, emit, join_room
import threading
import subprocess
import signal
import sys
import select
import logging
import shutil

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
        if user_
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
            {"$set": user_data, "$setOnInsert": {"bots": [], "email": email}},
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
    for bot_id, bot_info in list(running_bots.items()):
        try:
            process = bot_info["process"]
            if process.poll() is None:
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
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    try:
        response = requests.post(f"{DISCORD_API_BASE_URL}/oauth2/token", data=data, headers=headers)
        response.raise_for_status()
        token_data = response.json()
    except requests.RequestException as e:
        print(f"Error getting access token from Discord: {e}")
        return "Failed to retrieve access token.", 400
    
    access_token = token_data.get("access_token")
    if not access_token:
        print(f"Invalid token data from Discord: {token_data}")
        return "Failed to retrieve access token.", 400

    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        user_response = requests.get(f"{DISCORD_API_BASE_URL}/users/@me", headers=headers)
        user_response.raise_for_status()
        user_data = user_response.json()
    except requests.RequestException as e:
        print(f"Error fetching user data from Discord: {e}")
        return "Failed to fetch user data.", 400

    discord_id = user_data["id"]
    email = user_data.get("email")
    if not email:
        return "Email is required from Discord to log in.", 400
        
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
    needs_reload = False

    for i, server in enumerate(servers):
        bot_id = f"{current_user.id}_{server.get('server_name', i)}"
        bot_dir = os.path.join(BOT_WORKSPACES_PATH, bot_id)

        if not os.path.exists(bot_dir):
            os.makedirs(bot_dir)
            needs_reload = True
            
            requirements_content = "discord.py>=2.3.0\n"
            with open(os.path.join(bot_dir, "requirements.txt"), "w") as f:
                f.write(requirements_content)
            
            bot_template_content = '''import os
import discord

TOKEN = os.getenv("BOT_TOKEN")
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f"Logged in as {client.user} (ID: {client.user.id})")
    print("------")

@client.event
async def on_message(message):
    if message.author == client.user:
        return
    if message.content.startswith('$hello'):
        await message.channel.send('Hello!')

client.run(TOKEN)
'''
            with open(os.path.join(bot_dir, "bot_template.py"), "w", encoding="utf-8") as f:
                f.write(bot_template_content)

            new_startup_command = "python -u bot_template.py"
            db.users.update_one(
                {"_id": current_user.id},
                {"$set": {
                    f"servers.{i}.startup_command": new_startup_command,
                    f"servers.{i}.files": [
                        {"path": "requirements.txt", "type": "file"},
                        {"path": "bot_template.py", "type": "file"}
                    ]
                }}
            )

        server_status = "offline"
        if bot_id in running_bots and running_bots[bot_id]["process"].poll() is None:
            server_status = "online"
        
        if server.get("status") != server_status:
            needs_reload = True
            db.users.update_one(
                {"_id": current_user.id},
                {"$set": {f"servers.{i}.status": server_status}}
            )

    if needs_reload:
        user_data = db.users.find_one({"_id": current_user.id})
        servers = user_data.get("servers", [])

    return render_template("dashboard.html", user=current_user, servers=servers)

def build_file_tree(bot_dir, files_from_db):
    tree = {}
    for root, dirs, files in os.walk(bot_dir):
        # Create a relative path from the bot_dir
        relative_path = os.path.relpath(root, bot_dir)
        if relative_path == ".":
            current_level = tree
        else:
            parts = relative_path.split(os.sep)
            current_level = tree
            for part in parts:
                current_level = current_level.setdefault(part, {})
        
        for d in dirs:
            current_level.setdefault(d, {})
        for f in files:
            current_level[f] = 'file' # Placeholder
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
    bot_dir = os.path.join(BOT_WORKSPACES_PATH, bot_id)
    bot_data['_id'] = bot_id

    bot_data['status'] = "online" if bot_id in running_bots and running_bots[bot_id]["process"].poll() is None else "offline"
    bot_data['files_tree'] = build_file_tree(bot_dir, bot_data.get('files', []))
    
    return render_template("editor.html", bot=bot_data, user=current_user)

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

    full_path = os.path.normpath(os.path.join(bot_dir, path))
    if not full_path.startswith(os.path.abspath(bot_dir)):
        return jsonify({"error": "Invalid path"}), 403

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

def stream_bot_logs(bot_id, process):
    try:
        socketio.emit('log', {'data': f'[{datetime.datetime.now().strftime("%H:%M:%S")}] 🚀 Bot process started (PID: {process.pid})'}, room=bot_id)
        streams = [process.stdout, process.stderr]
        
        while process.poll() is None:
            readable, _, _ = select.select(streams, [], [], 0.1)
            for stream in readable:
                line = stream.readline()
                if line:
                    line = line.strip()
                    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
                    log_level = '[ERROR]' if stream is process.stderr else ''
                    socketio.emit('log', {'data': f'[{timestamp}] {log_level} {line}'.strip()}, room=bot_id)
            socketio.sleep(0.01)

        for stream in streams:
            for line in stream.readlines():
                line = line.strip()
                if line:
                    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
                    log_level = '[ERROR]' if stream is process.stderr else ''
                    socketio.emit('log', {'data': f'[{timestamp}] {log_level} {line}'.strip()}, room=bot_id)

        return_code = process.wait()
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        status_msg = '✅ Bot process finished successfully.' if return_code == 0 else f'⚠️ Bot process exited with code {return_code}.'
        socketio.emit('log', {'data': f'[{timestamp}] {status_msg}'}, room=bot_id)
    except Exception as e:
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        socketio.emit('log', {'data': f'[{timestamp}] 💥 Log streaming error: {str(e)}'}, room=bot_id)
    finally:
        if bot_id in running_bots:
            running_bots[bot_id]["status"] = "stopped"

@socketio.on('connect', namespace='/editor')
def editor_connect():
    emit('log', {'data': '🔌 Connected to console...'})

@socketio.on('join', namespace='/editor')
def on_join(data):
    bot_id = data.get('bot_id')
    if not bot_id: return
    join_room(bot_id)
    
    if bot_id in running_bots and running_bots[bot_id]["process"].poll() is None:
        emit('log', {'data': f'📡 Console reconnected - Bot {bot_id} is running.'}, room=bot_id)
    else:
        emit('log', {'data': f'📡 Console connected - Bot {bot_id} is offline.'}, room=bot_id)

@app.route("/api/server/<int:server_index>/start", methods=["POST"])
@login_required
def start_bot(server_index):
    user_data = db.users.find_one({"_id": current_user.id})
    servers = user_data.get("servers", [])
    if server_index >= len(servers):
        return jsonify({"error": "Server not found"}), 404

    bot_data = servers[server_index]
    bot_id = f"{current_user.id}_{bot_data.get('server_name', server_index)}"

    if bot_id in running_bots and running_bots[bot_id]["process"].poll() is None:
        socketio.emit('log', {'data': f'⚠️ Bot {bot_id} is already running.'}, room=bot_id)
        return jsonify({"error": "Bot is already running"}), 400
    
    bot_dir = os.path.join(BOT_WORKSPACES_PATH, bot_id)
    socketio.emit('log', {'data': f'🔄 Starting bot {bot_id}...'}, room=bot_id)

    token = bot_data.get("botToken")
    if not token or len(token.strip()) < 50:
        error_msg = f"❌ Invalid or missing BOT_TOKEN in database. Please check server settings."
        socketio.emit('log', {'data': error_msg}, room=bot_id)
        return jsonify({"error": "Invalid BOT_TOKEN"}), 400
    
    startup_file = os.path.join(bot_dir, "bot_template.py")
    if not os.path.isfile(startup_file):
        error_msg = f'❌ Bot startup file not found: {startup_file}'
        socketio.emit('log', {'data': error_msg}, room=bot_id)
        return jsonify({"error": "Bot startup file not found"}), 500

    requirements_file = os.path.join(bot_dir, "requirements.txt")
    if os.path.exists(requirements_file):
        socketio.emit('log', {'data': '📦 Installing dependencies...'}, room=bot_id)
        try:
            install_command = [sys.executable, "-m", "pip", "install", "-r", requirements_file]
            install_process = subprocess.run(install_command, cwd=bot_dir, capture_output=True, text=True, timeout=300)
            if install_process.returncode != 0:
                socketio.emit('log', {'data': f'[pip-error] {install_process.stderr}'}, room=bot_id)
                return jsonify({"error": "Failed to install dependencies"}), 500
            socketio.emit('log', {'data': '✅ Dependencies installed successfully.'}, room=bot_id)
        except Exception as e:
            socketio.emit('log', {'data': f'❌ Error during dependency installation: {str(e)}'}, room=bot_id)
            return jsonify({"error": "Dependency installation failed"}), 500

    startup_command_str = bot_data.get("startup_command", "python -u bot_template.py")
    startup_command = startup_command_str.split()
    socketio.emit('log', {'data': f'🚀 Executing: {startup_command_str}'}, room=bot_id)

    try:
        env = dict(os.environ)
        env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "BOT_TOKEN": token, "BOT_ID": bot_id})

        process = subprocess.Popen(
            startup_command,
            cwd=bot_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            universal_newlines=True,
            env=env
        )

        log_thread = threading.Thread(target=stream_bot_logs, args=(bot_id, process), daemon=True)
        log_thread.start()

        running_bots[bot_id] = {
            "process": process, "thread": log_thread, "status": "running",
            "started_at": datetime.datetime.now(), "command": startup_command_str
        }
        return jsonify({"success": True, "pid": process.pid})
    except Exception as e:
        socketio.emit('log', {'data': f'💥 Failed to start bot: {str(e)}'}, room=bot_id)
        return jsonify({"error": f"Failed to start bot: {str(e)}"}), 500

@app.route("/api/server/<int:server_index>/stop", methods=["POST"])
@login_required
def stop_bot(server_index):
    user_data = db.users.find_one({"_id": current_user.id})
    servers = user_data.get("servers", [])
    if server_index >= len(servers):
        return jsonify({"error": "Server not found"}), 404

    bot_data = servers[server_index]
    bot_id = f"{current_user.id}_{bot_data.get('server_name', server_index)}"

    if bot_id not in running_bots or running_bots[bot_id]["process"].poll() is not None:
        socketio.emit('log', {'data': f'⚠️ Bot {bot_id} is not running.'}, room=bot_id)
        if bot_id in running_bots: del running_bots[bot_id]
        return jsonify({"error": "Bot is not running"}), 400

    process = running_bots[bot_id]["process"]
    socketio.emit('log', {'data': f'🛑 Stopping bot {bot_id} (PID: {process.pid})...'}, room=bot_id)

    try:
        process.terminate()
        process.wait(timeout=10)
        socketio.emit('log', {'data': '✅ Bot stopped gracefully.'}, room=bot_id)
    except subprocess.TimeoutExpired:
        socketio.emit('log', {'data': '⚠️ Forcing termination...'}, room=bot_id)
        process.kill()
        process.wait()
        socketio.emit('log', {'data': '💀 Bot terminated forcefully.'}, room=bot_id)
    except Exception as e:
        socketio.emit('log', {'data': f'💥 Error stopping bot: {str(e)}'}, room=bot_id)
    
    if bot_id in running_bots:
        del running_bots[bot_id]
    return jsonify({"success": True})

if __name__ == "__main__":
    try:
        print("🚀 Starting Flask application...")
        socketio.run(app, host='0.0.0.0', port=30158, debug=True, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        cleanup_bot_processes()
