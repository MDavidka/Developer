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
import traceback

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

# MongoDB setup
if os.environ.get("FLASK_ENV") == "testing":
    from mongomock import MongoClient
    mongo_client = MongoClient()
    db = mongo_client['dash-bot-test']
else:
    from pymongo import MongoClient
    mongo_client = MongoClient(os.getenv("MONGO_URI"))
    db = mongo_client['dash-bot']
    # Clear any stale processes on startup
    if 'bot_processes' in db.list_collection_names():
        db.bot_processes.delete_many({})

# Bot process management
running_processes = {}

# Discord OAuth2 settings
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "http://localhost:5000/callback")
DISCORD_API_BASE_URL = "https://discord.com/api"
DISCORD_AUTHORIZATION_URL = f"https://discord.com/api/oauth2/authorize?client_id={DISCORD_CLIENT_ID}&redirect_uri={DISCORD_REDIRECT_URI}&response_type=code&scope=identify%20email"

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
    for bot_process in db.bot_processes.find():
        try:
            os.kill(bot_process['pid'], signal.SIGTERM)
            print(f"Terminated bot {bot_process['bot_id']} (PID: {bot_process['pid']})")
        except OSError:
            pass # Process already dead
    db.bot_processes.delete_many({})

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
            template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bot_template.py')
            with open(template_path, 'r') as f:
                bot_template_content = f.read()

            main_py_content = "from dotenv import load_dotenv\nload_dotenv()\n\n" + bot_template_content
            with open(os.path.join(bot_dir, 'main.py'), 'w') as f:
                f.write(main_py_content)

            # Create .env file
            with open(os.path.join(bot_dir, '.env'), 'w') as f:
                f.write(f"BOT_TOKEN={server.get('botToken', '')}")

            # Update database
            new_files = [
                {"path": "main.py", "type": "file"},
                {"path": ".env", "type": "file"}
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
        bot_process = db.bot_processes.find_one({"bot_id": bot_id})
        server_status = "offline"
        if bot_process:
            try:
                os.kill(bot_process['pid'], 0)
                server_status = "online"
            except OSError:
                db.bot_processes.delete_one({"bot_id": bot_id})

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
            current_level[path_parts[-1]] = 'file'
        # We don't need to handle folders explicitly, as setdefault does it.

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
    bot_process = db.bot_processes.find_one({"bot_id": bot_id})
    bot_data['status'] = "offline"
    if bot_process:
        try:
            os.kill(bot_process['pid'], 0)
            bot_data['status'] = "online"
        except OSError:
            db.bot_processes.delete_one({"bot_id": bot_id})

    bot_data['_id'] = bot_id
    bot_data['server_index'] = server_index

    file_tree = build_file_tree(bot_data.get('files', []))
    bot_data['files_tree'] = file_tree

    with open('file_info.json', 'r') as f:
        file_info = json.load(f)
    bot_data['file_info'] = file_info

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
                {"$push": {"servers.$.files": {"path": path, "type": "file"}}}
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
        db.bot_processes.delete_one({"bot_id": bot_id})

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
    bot_process = db.bot_processes.find_one({"bot_id": bot_id})
    if bot_process:
        try:
            os.kill(bot_process['pid'], 0)
            emit('log', {'data': f'📡 Console connected - Bot {bot_id} is running (PID: {bot_process["pid"]})'}, room=bot_id)
        except OSError:
            db.bot_processes.delete_one({"bot_id": bot_id})
            emit('log', {'data': f'📡 Console connected - Bot {bot_id} is stopped'}, room=bot_id)
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
    bot_process = db.bot_processes.find_one({"bot_id": bot_id})
    if bot_process:
        try:
            os.kill(bot_process['pid'], 0)
            logger.warning(f"Bot {bot_id} is already running (PID: {bot_process['pid']}). Aborting start.")
            socketio.emit('log', {'data': f'⚠️ Bot {bot_id} is already running (PID: {bot_process["pid"]})'}, room=bot_id)
            return jsonify({"error": "Bot is already running"}), 400
        except OSError:
            logger.info(f"Bot {bot_id} had a stale process record. Cleaning up.")
            db.bot_processes.delete_one({"bot_id": bot_id})

    bot_dir = os.path.join(BOT_WORKSPACES_PATH, bot_id)
    logger.info(f"Bot workspace directory: {os.path.abspath(bot_dir)}")

    main_py_path = os.path.join(bot_dir, "main.py")
    logger.info(f"Executing script: {os.path.abspath(main_py_path)}")


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

        running_processes[bot_id] = process

        db.bot_processes.insert_one({
            "bot_id": bot_id,
            "pid": process.pid,
            "started_at": datetime.datetime.now(),
            "command": " ".join(startup_command)
        })

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

    bot_process = db.bot_processes.find_one({"bot_id": bot_id})
    if not bot_process:
        socketio.emit('log', {
            'data': f'⚠️ Bot {bot_id} is not running'
        }, room=bot_id)
        return jsonify({"error": "Bot is not running"}), 400

    pid = bot_process['pid']
    socketio.emit('log', {
        'data': f'🛑 Stopping bot {bot_id} (PID: {pid})...'
    }, room=bot_id)

    if bot_id in running_processes:
        process = running_processes[bot_id]
        try:
            process.terminate()
            try:
                process.wait(timeout=10)
                socketio.emit('log', {'data': '✅ Bot stopped gracefully'}, room=bot_id)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                socketio.emit('log', {'data': '💀 Bot terminated forcefully'}, room=bot_id)
            del running_processes[bot_id]
        except Exception as e:
            logger.error(f"Error stopping bot {bot_id} with PID {pid}: {e}")
            socketio.emit('log', {'data': f'💥 Error stopping bot: {e}'}, room=bot_id)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
            socketio.emit('log', {'data': '✅ Bot stopped gracefully (by PID)'}, room=bot_id)
        except OSError:
            socketio.emit('log', {'data': f'⚠️ Bot process with PID {pid} not found.'}, room=bot_id)

    # Clean up
    db.bot_processes.delete_one({"bot_id": bot_id})

    return jsonify({"success": True})

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

    bot_process = db.bot_processes.find_one({"bot_id": bot_id})
    if bot_process:
        try:
            os.kill(bot_process['pid'], 0)
            return jsonify({
                "status": "running",
                "pid": bot_process['pid'],
                "started_at": bot_process['started_at'].isoformat(),
                "command": bot_process['command']
            })
        except OSError:
            db.bot_processes.delete_one({"bot_id": bot_id})

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

import google.generativeai as genai

@app.route("/api/server/<int:server_index>/ai-identify-files", methods=["POST"])
@login_required
def ai_identify_files(server_index):
    logger.info(f"AI identify files request for server {server_index} by user {current_user.id}")

    # Get the user's prompt from the request
    prompt = request.json.get("prompt")
    if not prompt:
        return jsonify({"error": "Prompt is required"}), 400

    # Configure the Google AI API
    google_api_key = os.getenv("GOOGLE_API_KEY")
    if not google_api_key:
        logger.error("GOOGLE_API_KEY not found in environment variables.")
        return jsonify({"error": "AI service is not configured."}), 500

    genai.configure(api_key=google_api_key)
    model = genai.GenerativeModel('gemini-1.5-flash-latest')

    # Get the bot's data
    user_data = db.users.find_one({"_id": current_user.id})
    servers = user_data.get("servers", [])
    if server_index >= len(servers):
        return jsonify({"error": "Server not found"}), 404

    bot_data = servers[server_index]

    # Get the file list
    files = bot_data.get('files', [])
    file_paths = [file['path'] for file in files]

    # Construct the prompt for the AI
    ai_prompt = f"""You are an expert code architect. Based on the user's request and the following file list, identify which files need to be edited.

User request: "{prompt}"

File list:
{json.dumps(file_paths, indent=2)}

Your response must be a JSON array of strings, where each string is a file path from the list above.
For example:
["main.py", "utils/helpers.py"]
"""

    try:
        # Call the Google AI API
        response = model.generate_content(ai_prompt)

        # Log the raw response for debugging
        logger.info(f"AI response: {response.text}")

        # Extract the JSON from the response
        text_response = response.text
        if '```json' in text_response:
            text_response = text_response.split('```json')[1].split('```')[0]

        # Parse the AI's response
        try:
            identified_files = json.loads(text_response)
        except json.JSONDecodeError:
            logger.error("Failed to decode JSON from AI response.")
            return jsonify({"error": "The AI returned an invalid response. Please try again."}), 500

        return jsonify({
            "success": True,
            "files": identified_files
        })

    except Exception as e:
        logger.error(f"Error during AI file identification: {traceback.format_exc()}")
        return jsonify({"error": "An error occurred during AI file identification."}), 500

@app.route("/api/server/<int:server_index>/ai-edit", methods=["POST"])
@login_required
def ai_edit(server_index):
    logger.info(f"AI edit request for server {server_index} by user {current_user.id}")

    # Get the user's prompt from the request
    prompt = request.json.get("prompt")
    if not prompt:
        return jsonify({"error": "Prompt is required"}), 400

    identified_files = request.json.get("files", [])

    # Configure the Google AI API
    google_api_key = os.getenv("GOOGLE_API_KEY")
    if not google_api_key:
        logger.error("GOOGLE_API_KEY not found in environment variables.")
        return jsonify({"error": "AI service is not configured."}), 500

    genai.configure(api_key=google_api_key)
    model = genai.GenerativeModel('gemini-1.5-flash-latest')

    # Get the bot's data
    user_data = db.users.find_one({"_id": current_user.id})
    servers = user_data.get("servers", [])
    if server_index >= len(servers):
        return jsonify({"error": "Server not found"}), 404

    bot_data = servers[server_index]
    bot_id = f"{current_user.id}_{bot_data.get('server_name', server_index)}"
    bot_dir = os.path.join(BOT_WORKSPACES_PATH, bot_id)

    # Get the file list and their contents
    files = bot_data.get('files', [])
    file_contents = {}
    for file in files:
        try:
            with open(os.path.join(bot_dir, file['path']), 'r') as f:
                file_contents[file['path']] = f.read()
        except Exception as e:
            logger.error(f"Error reading file {file['path']}: {e}")
            return jsonify({"error": f"Error reading file {file['path']}"}), 500

    # Construct the enhanced prompt for the AI
    ai_prompt = f"""You are an expert Python Discord bot developer with advanced knowledge of code architecture, best practices, and optimization techniques. A user wants to make the following change to their project:
{prompt}

**VERY IMPORTANT INSTRUCTIONS:**
1. Do not put all the code in `main.py`. Follow proper separation of concerns.
2. For new commands or features, you **must** create new files inside a `functions` directory. If the directory does not exist, you can create it.
3. This is a Discord.py project that uses Cogs for organizing commands. New commands should be in their own cog file inside the `functions` directory.
4. After creating a new cog file in `functions/`, you **must** update `main.py` to load the new cog.
5. Follow Python best practices: proper error handling, type hints, docstrings, and clean code principles.
6. Use async/await properly for Discord.py operations.
7. Include proper logging and error handling.
8. Optimize code for performance and maintainability.
9. Add comprehensive comments for complex logic.
10. Ensure all imports are properly organized and necessary.

**CURRENT PROJECT CONTEXT:**
Here is the current file structure and content of the project:
{json.dumps(file_contents, indent=2)}

**FILES TO EDIT:**
Based on a previous analysis, you should focus on editing the following files:
{json.dumps(identified_files, indent=2)}

**RESPONSE FORMAT:**
Your response must be a JSON object where the keys are the file paths and the values are the new, complete code for that file. Include only the files that need to be created or modified.

**EXAMPLE STRUCTURE:**
{{
  "functions/new_cog.py": "import discord\\nfrom discord.ext import commands\\nimport logging\\n\\nlogger = logging.getLogger(__name__)\\n\\nclass NewCog(commands.Cog):\\n    def __init__(self, bot):\\n        self.bot = bot\\n\\n    @commands.command(name='newcommand')\\n    async def new_command(self, ctx):\\n        \\\"\\\"\\\"A new command that does something useful.\\\"\\\"\\\"\\n        try:\\n            await ctx.send('This is a new command!')\\n        except Exception as e:\\n            logger.error(f'Error in new_command: {{e}}')\\n            await ctx.send('An error occurred while executing the command.')\\n\\ndef setup(bot):\\n    bot.add_cog(NewCog(bot))",
  "main.py": "# ... (existing main.py code) ...\\n# Add this at the end to load the cog\\nbot.load_extension('functions.new_cog')"
}}

**CODE QUALITY REQUIREMENTS:**
- Use proper error handling with try-catch blocks
- Include type hints where appropriate
- Add docstrings for all functions and classes
- Use meaningful variable and function names
- Follow PEP 8 style guidelines
- Include logging for debugging and monitoring
- Optimize for performance and readability
"""

    try:
        # Call the Google AI API
        response = model.generate_content(ai_prompt)

        # Log the raw response for debugging
        logger.info(f"AI response: {response.text}")

        # Extract the JSON from the response
        text_response = response.text
        if '```json' in text_response:
            text_response = text_response.split('```json')[1].split('```')[0]

        # Parse the AI's response
        try:
            edited_files = json.loads(text_response)
        except json.JSONDecodeError:
            logger.error("Failed to decode JSON from AI response.")
            return jsonify({"error": "The AI returned an invalid response. Please try again."}), 500

        # Apply the changes
        modified_files = []
        for filepath, new_content in edited_files.items():
            full_path = os.path.join(bot_dir, filepath)

            # Create directories if they don't exist
            os.makedirs(os.path.dirname(full_path), exist_ok=True)

            with open(full_path, 'w') as f:
                f.write(new_content)

            # Check if the file is new
            is_new_file = not any(f['path'] == filepath for f in files)

            if is_new_file:
                # Add the new file to the database
                db.users.update_one(
                    {"_id": current_user.id, "servers.server_name": bot_data['server_name']},
                    {"$push": {"servers.$.files": {"path": filepath, "type": "file"}}}
                )
                modified_files.append(f"Created: {filepath}")
            else:
                modified_files.append(f"Modified: {filepath}")

        return jsonify({
            "success": True, 
            "modified_files": modified_files,
            "message": f"Successfully processed {len(modified_files)} files"
        })

    except Exception as e:
        logger.error(f"Error during AI edit: {traceback.format_exc()}")
        return jsonify({"error": "An error occurred during the AI edit."}), 500

if __name__ == "__main__":
    try:
        print("🚀 Starting Flask application...")
        print(f"BOT_WORKSPACES_PATH: {BOT_WORKSPACES_PATH}")
        socketio.run(app, host='0.0.0.0', port=30158, debug=True, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        print("\nShutting down...")
        cleanup_bot_processes()
