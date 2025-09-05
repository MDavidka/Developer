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
    for bot_id, bot_info in list(running_bots.items()):
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
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    response = requests.post(f"{DISCORD_API_BASE_URL}/oauth2/token", data=data, headers=headers)
    token_data = response.json()
    access_token = token_data.get("access_token")

    if not access_token:
        print(f"Error getting access token from Discord: {token_data}")
        return "Failed to retrieve access token.", 400

    headers = {"Authorization": f"Bearer {access_token}"}
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

            # Create default requirements.txt
            requirements_content = "discord.py>=2.3.0\npython-dotenv>=1.0.0\naiohttp>=3.8.0\n"
            with open(os.path.join(bot_dir, "requirements.txt"), "w") as f:
                f.write(requirements_content)
            
            # **FIX**: Create default bot_template.py
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


            # Set the new default startup command and update files array
            new_startup_command = "python -u bot_template.py"
            db.users.update_one(
                {"_id": current_user.id, f"servers.{i}": server},
                {"$set": {
                    f"servers.{i}.startup_command": new_startup_command,
                    f"servers.{i}.files": [
                        {"path": "requirements.txt", "type": "file", "content": requirements_content},
                        {"path": "bot_template.py", "type": "file", "content": bot_template_content}
                    ]
                }}
            )

        # Update server status
        server_status = "offline"
        if bot_id in running_bots:
            process = running_bots[bot_id]["process"]
            if process.poll() is None:
                server_status = "online"
            else:
                del running_bots[bot_id]
        
        if server.get("status") != server_status:
            db.users.update_one(
                {"_id": current_user.id, f"servers.{i}": server},
                {"$set": {f"servers.{i}.status": server_status}}
            )

    user_data = db.users.find_one({"_id": current_user.id})
    servers = user_data.get("servers", [])
    return render_template("dashboard.html", user=current_user, servers=servers)

def build_file_tree(file_list):
    tree = {}
    for file_doc in sorted(file_list, key=lambda x: x['path']):
        path_parts = file_doc['path'].split('/')
        current_level = tree
        for part in path_parts[:-1]:
            current_level = current_level.setdefault(part, {})
        if file_doc['type'] == 'file':
            current_level[path_parts[-1]] = file_doc.get('content', '')
        else: # folder
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
    bot_data['_id'] = bot_id

    if bot_id in running_bots and running_bots[bot_id]["process"].poll() is None:
        bot_data['status'] = "online"
    else:
        bot_data['status'] = "offline"

    file_tree = build_file_tree(bot_data.get('files', []))
    bot_data['files_tree'] = file_tree
    
    return render_template("editor.html", bot=bot_data, user=current_user)

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
    
    debug_info = {"bot_dir": bot_dir, "bot_id": bot_id}
    token = bot_data.get("botToken")

    if token:
        debug_info.update({
            "token_in_db": True,
            "token_length": len(token),
            "token_preview": f"{token[:10]}...{token[-4:]}" if len(token) > 14 else "too_short",
            "token_valid_length": len(token) >= 59 # Discord tokens are typically longer
        })
    else:
        debug_info.update({"token_in_db": False})
    
    try:
        response = requests.get("https://discord.com/api/v10/gateway", timeout=10)
        debug_info["discord_api"] = {"reachable": response.status_code == 200, "status_code": response.status_code}
    except Exception as e:
        debug_info["discord_api"] = {"reachable": False, "error": str(e)}
    
    return jsonify(debug_info)

# ... (other routes for file management remain the same) ...

def stream_bot_logs(bot_id, process):
    """Enhanced log streaming with non-blocking reads."""
    try:
        socketio.emit('log', {'data': f'[{datetime.datetime.now().strftime("%H:%M:%S")}] 🚀 Bot process started (PID: {process.pid})'}, room=bot_id)
        streams = [process.stdout, process.stderr]
        
        while process.poll() is None:
            readable, _, _ = select.select(streams, [], [], 0.1)
            for stream in readable:
                line = stream.readline().strip()
                if line:
                    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
                    log_level = '[ERROR]' if stream is process.stderr else ''
                    socketio.emit('log', {'data': f'[{timestamp}] {log_level} {line}'.strip()}, room=bot_id)
            socketio.sleep(0.01) # prevent tight loop

        # Read any remaining output
        for stream in streams:
            for line in stream.readlines():
                line = line.strip()
                if line:
                    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
                    log_level = '[ERROR]' if stream is process.stderr else ''
                    socketio.emit('log', {'data': f'[{timestamp}] {log_level} {line}'.strip()}, room=bot_id)

        return_code = process.returncode
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
    bot_id = data['bot_id']
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

    # Pre-flight checks
    if not os.path.isdir(bot_dir):
        error_msg = f'❌ Workspace directory not found: {bot_dir}'
        socketio.emit('log', {'data': error_msg}, room=bot_id)
        return jsonify({"error": "Workspace not found"}), 404

    token = bot_data.get("botToken")
    if not token or len(token.strip()) < 59:
        error_msg = f"❌ Invalid or missing BOT_TOKEN in database. Please check server settings."
        socketio.emit('log', {'data': error_msg}, room=bot_id)
        return jsonify({"error": "Invalid BOT_TOKEN"}), 400
    socketio.emit('log', {'data': '✅ Token validation passed.'}, room=bot_id)
    
    # Check bot_template.py exists
    startup_file = os.path.join(bot_dir, "bot_template.py")
    if not os.path.isfile(startup_file):
        error_msg = f'❌ Bot startup file not found: {startup_file}'
        socketio.emit('log', {'data': error_msg}, room=bot_id)
        return jsonify({"error": "Bot startup file not found"}), 500

    # Install dependencies
    requirements_file = os.path.join(bot_dir, "requirements.txt")
    if os.path.exists(requirements_file):
        socketio.emit('log', {'data': '📦 Installing dependencies from requirements.txt...'}, room=bot_id)
        try:
            install_command = [sys.executable, "-m", "pip", "install", "-r", requirements_file]
            install_process = subprocess.run(
                install_command, cwd=bot_dir, capture_output=True, text=True, encoding='utf-8', timeout=300
            )
            if install_process.stdout:
                for line in install_process.stdout.splitlines():
                    socketio.emit('log', {'data': f'[pip] {line}'}, room=bot_id)
            if install_process.returncode != 0:
                if install_process.stderr:
                    for line in install_process.stderr.splitlines():
                        socketio.emit('log', {'data': f'[pip-error] {line}'}, room=bot_id)
                error_msg = '❌ Failed to install dependencies.'
                socketio.emit('log', {'data': error_msg}, room=bot_id)
                return jsonify({"error": "Failed to install dependencies"}), 500
            socketio.emit('log', {'data': '✅ Dependencies installed successfully.'}, room=bot_id)
        except Exception as e:
            error_msg = f'❌ Error during dependency installation: {str(e)}'
            socketio.emit('log', {'data': error_msg}, room=bot_id)
            return jsonify({"error": "Dependency installation failed"}), 500

    startup_command_str = bot_data.get("startup_command", "python -u bot_template.py")
    startup_command = startup_command_str.split()
    socketio.emit('log', {'data': f'🚀 Executing: {startup_command_str}'}, room=bot_id)

    try:
        env = dict(os.environ)
        env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "BOT_TOKEN": token, "BOT_ID": bot_id})

        process = subprocess.Popen(
            startup_command,
            # **FIX**: Set current working directory to the bot's workspace
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

        socketio.emit('log', {'data': f'✅ Bot subprocess started (PID: {process.pid})'}, room=bot_id)
        return jsonify({"success": True, "pid": process.pid, "command": startup_command_str})

    except Exception as e:
        error_msg = f'💥 Failed to start bot subprocess: {str(e)}'
        socketio.emit('log', {'data': error_msg}, room=bot_id)
        return jsonify({"error": f"Failed to start bot subprocess: {str(e)}"}), 500

# ... (stop_bot, bot_status, and package management routes remain the same) ...

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
        # Clean up if entry exists for a dead process
        if bot_id in running_bots:
            del running_bots[bot_id]
        return jsonify({"error": "Bot is not running"}), 400

    bot_info = running_bots[bot_id]
    process = bot_info["process"]
    socketio.emit('log', {'data': f'🛑 Stopping bot {bot_id} (PID: {process.pid})...'}, room=bot_id)

    try:
        process.terminate()
        try:
            process.wait(timeout=10)
            socketio.emit('log', {'data': '✅ Bot stopped gracefully.'}, room=bot_id)
        except subprocess.TimeoutExpired:
            socketio.emit('log', {'data': '⚠️ Bot did not stop gracefully, forcing termination...'}, room=bot_id)
            process.kill()
            process.wait()
            socketio.emit('log', {'data': '💀 Bot terminated forcefully.'}, room=bot_id)

        del running_bots[bot_id]
        return jsonify({"success": True})

    except Exception as e:
        error_msg = f'💥 Error stopping bot: {str(e)}'
        socketio.emit('log', {'data': error_msg}, room=bot_id)
        # Ensure cleanup even on error
        if bot_id in running_bots:
            del running_bots[bot_id]
        return jsonify({"error": f"Error stopping bot: {str(e)}"}), 500

if __name__ == "__main__":
    try:
        print("🚀 Starting Flask application...")
        print(f"BOT_WORKSPACES_PATH: {os.path.abspath(BOT_WORKSPACES_PATH)}")
        socketio.run(app, host='0.0.0.0', port=30158, debug=True, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        cleanup_bot_processes()
