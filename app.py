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

            # Create main.py
            with open('bot_template.py', 'r') as f:
                bot_template_content = f.read()

            main_py_content = "from dotenv import load_dotenv\nload_dotenv()\n\n" + bot_template_content
            with open(os.path.join(bot_dir, 'main.py'), 'w') as f:
                f.write(main_py_content)

            # Create .env file
            with open(os.path.join(bot_dir, '.env'), 'w') as f:
                f.write(f"BOT_TOKEN={server.get('botToken', '')}")

            # Update database
            new_files = [
                {"path": "main.py", "type": "file", "content": main_py_content},
                {"path": ".env", "type": "file", "content": f"BOT_TOKEN={server.get('botToken', '')}"}
            ]

            db.users.update_one(
                {"_id": current_user.id, "servers.server_name": server['server_name']},
                {
                    "$set": {
                        f"servers.{i}.startup_command": "python -u main.py",
                        f"servers.{i}.files": new_files
                    }
                }
            )

        # Update server status based on running processes
        if bot_id in running_bots:
            process = running_bots[bot_id]["process"]
            if process.poll() is None:
                server_status = "online"
            else:
                server_status = "offline"
                del running_bots[bot_id]
        else:
            server_status = "offline"

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

    bot_data['_id'] = bot_id
    bot_data['server_index'] = server_index

    return render_template("editor.html", bot=bot_data, user=current_user, files=bot_data.get('files', []))

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
    logger.info(f"File content requested for server {server_index} by user {current_user.id}")
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
        logger.info(f"Reading file: {full_path}")
        try:
            with open(full_path, "r", encoding='utf-8') as f:
                content = f.read()
            logger.info(f"File read successfully: {full_path}")
            return jsonify({"content": content})
        except FileNotFoundError:
            logger.warning(f"File not found: {full_path}")
            return jsonify({"error": "File not found"}), 404
        except Exception as e:
            logger.error(f"Error reading file {full_path}: {e}")
            return jsonify({"error": f"Error reading file: {str(e)}"}), 500

    if request.method == "POST":
        logger.info(f"Writing to file: {full_path}")
        try:
            content = request.json.get("content", "")
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding='utf-8') as f:
                f.write(content)
            logger.info(f"File saved successfully: {full_path}")
            return jsonify({"success": True})
        except Exception as e:
            logger.error(f"Error saving file {full_path}: {e}")
            return jsonify({"error": f"Error saving file: {str(e)}"}), 500

@app.route("/api/server/<int:server_index>/files/create", methods=["POST"])
@login_required
def create_file(server_index):
    logger.info(f"File creation requested for server {server_index} by user {current_user.id}")
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
            db.users.update_one(
                {"_id": current_user.id, "servers.server_name": bot_data['server_name']},
                {"$push": {"servers.$.files": {"path": path, "type": "folder"}}}
            )
        else:
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding='utf-8') as f:
                f.write("")
            db.users.update_one(
                {"_id": current_user.id, "servers.server_name": bot_data['server_name']},
                {"$push": {"servers.$.files": {"path": path, "type": "file", "content": ""}}}
            )
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": f"Error creating file/folder: {str(e)}"}), 500

@app.route("/api/server/<int:server_index>/files/delete", methods=["POST"])
@login_required
def delete_file(server_index):
    logger.info(f"File deletion requested for server {server_index} by user {current_user.id}")
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
            # Remove the folder and all its contents from the database
            db.users.update_one(
                {"_id": current_user.id, "servers.server_name": bot_data['server_name']},
                {"$pull": {"servers.$.files": {"path": {"$regex": f"^{path}(/.*)?$"}}}}
            )
        elif os.path.isfile(full_path):
            os.remove(full_path)
            db.users.update_one(
                {"_id": current_user.id, "servers.server_name": bot_data['server_name']},
                {"$pull": {"servers.$.files": {"path": path}}}
            )
        else:
            return jsonify({"error": "File or folder not found"}), 404
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": f"Error deleting file/folder: {str(e)}"}), 500

def stream_bot_logs(bot_id, process):
    """Enhanced log streaming with proper process management using select"""
    logger.info(f"Starting log stream for bot_id: {bot_id}")
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
                    log_line = f'[{timestamp}] {log_level} {line}'.strip()
                    socketio.emit('log', {'data': log_line}, room=bot_id)
                    print(f"[BOT_LOGS/{bot_id}] {log_line}") # Mirror to main console

        # After process finishes, read any remaining output
        for stream in streams:
            for line in stream.readlines():
                line = line.strip()
                if line:
                    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
                    log_level = '[ERROR]' if stream is process.stderr else ''
                    log_line = f'[{timestamp}] {log_level} {line}'.strip()
                    socketio.emit('log', {'data': log_line}, room=bot_id)
                    print(f"[BOT_LOGS/{bot_id}] {log_line}") # Mirror to main console

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
    logger.info(f"Client connected to /editor namespace: {request.sid}")
    emit('log', {'data': '🔌 Connected to console...'})

@socketio.on('join', namespace='/editor')
def on_join(data):
    bot_id = data['bot_id']
    logger.info(f"Client {request.sid} joining room {bot_id}")
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
    logger.info(f"Start bot request for server {server_index} by user {current_user.id}")
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
            socketio.emit('log', {'data': f'⚠️ Bot {bot_id} is already running (PID: {process.pid})'}, room=bot_id)
            return jsonify({"error": "Bot is already running"}), 400
        else:
            del running_bots[bot_id]

    bot_dir = os.path.join(BOT_WORKSPACES_PATH, bot_id)

    socketio.emit('log', {'data': f'🔄 Starting bot {bot_id}...'}, room=bot_id)

    # Pre-flight checks
    if not os.path.isdir(bot_dir):
        error_msg = f'❌ Workspace directory not found: {bot_dir}'
        socketio.emit('log', {'data': error_msg}, room=bot_id)
        return jsonify({"error": "Workspace not found"}), 404

    # The bot will use the main application's environment.

    socketio.emit('log', {'data': f'✅ Pre-flight checks passed'}, room=bot_id)
    
    startup_command = [sys.executable, "-u", "main.py"]
    socketio.emit('log', {'data': f'🚀 Executing: {" ".join(startup_command)}'}, room=bot_id)

    try:
        # Create subprocess with proper environment
        env = dict(os.environ)
        env.update({
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            "BOT_ID": bot_id
        })

        process = subprocess.Popen(
            startup_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            universal_newlines=True,
            env=env,
            cwd=bot_dir
        )

        log_thread = threading.Thread(target=stream_bot_logs, args=(bot_id, process), daemon=True)
        log_thread.start()

        running_bots[bot_id] = {
            "process": process,
            "thread": log_thread,
            "status": "running",
            "started_at": datetime.datetime.now(),
            "command": " ".join(startup_command)
        }

        socketio.emit('log', {'data': f'✅ Bot subprocess started (PID: {process.pid})'}, room=bot_id)

        return jsonify({"success": True, "pid": process.pid, "command": " ".join(startup_command)})

    except Exception as e:
        error_msg = f'💥 Failed to start bot subprocess: {str(e)}'
        socketio.emit('log', {'data': error_msg}, room=bot_id)
        return jsonify({"error": f"Failed to start bot subprocess: {str(e)}"}), 500

@app.route("/api/server/<int:server_index>/stop", methods=["POST"])
@login_required
def stop_bot(server_index):
    logger.info(f"Stop bot request for server {server_index} by user {current_user.id}")
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
    logger.info(f"Bot status request for server {server_index} by user {current_user.id}")
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

if __name__ == "__main__":
    try:
        print("🚀 Starting Flask application...")
        print(f"BOT_WORKSPACES_PATH: {BOT_WORKSPACES_PATH}")
        socketio.run(app, host='0.0.0.0', port=30158, debug=True, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        print("\nShutting down...")
        cleanup_bot_processes()
