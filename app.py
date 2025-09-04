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

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY")
socketio = SocketIO(app, cors_allowed_origins="*")

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

            # Auto-generate main.py
            main_py_content = f"""
from dotenv import load_dotenv
import os
import discord

load_dotenv()

token = os.getenv('BOT_TOKEN')
if not token:
    raise ValueError("BOT_TOKEN not found in .env file")

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f'We have logged in as {{client.user}}')

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    if message.content.startswith('$hello'):
        await message.channel.send('Hello!')

client.run(token)
"""
            with open(os.path.join(bot_dir, "main.py"), "w") as f:
                f.write(main_py_content)

            # Set default startup command in the database
            db.users.update_one(
                {"_id": current_user.id},
                {"$set": {f"servers.{i}.startup_command": "python -u main.py"}}
            )
            # Add files array to the server object
            db.users.update_one(
                {"_id": current_user.id},
                {"$set": {f"servers.{i}.files": [{"path": "main.py", "type": "file", "content": main_py_content}, {"path": ".env", "type": "file", "content": f"BOT_TOKEN={server.get('botToken', '')}"}]}}
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
    """Enhanced log streaming with proper process management"""
    try:
        socketio.emit('log', {
            'data': f'[{datetime.datetime.now().strftime("%H:%M:%S")}] 🚀 Bot process started (PID: {process.pid})'
        }, room=bot_id)

        # Read stdout and stderr simultaneously
        import select
        
        while True:
            # Check if process is still running
            if process.poll() is not None:
                break
                
            # Use select to check for available data (Unix-like systems)
            if hasattr(select, 'select'):
                ready, _, _ = select.select([process.stdout, process.stderr], [], [], 0.1)
                
                for stream in ready:
                    line = stream.readline()
                    if line:
                        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
                        if stream == process.stderr:
                            socketio.emit('log', {
                                'data': f'[{timestamp}] ❌ {line.strip()}'
                            }, room=bot_id)
                        else:
                            socketio.emit('log', {
                                'data': f'[{timestamp}] ℹ️ {line.strip()}'
                            }, room=bot_id)
            else:
                # Fallback for Windows
                line = process.stdout.readline()
                if line:
                    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
                    socketio.emit('log', {
                        'data': f'[{timestamp}] ℹ️ {line.strip()}'
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
        if bot_id in running_bots:
            running_bots[bot_id]["status"] = "stopped"
            # Don't delete immediately, let the stop endpoint handle cleanup

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

    # Enhanced startup sequence with better error handling
    socketio.emit('log', {
        'data': f'🔄 Starting bot {bot_id}...'
    }, room=bot_id)

    # Pre-flight checks
    if not os.path.isdir(bot_dir):
        error_msg = f'❌ Workspace directory not found: {bot_dir}'
        socketio.emit('log', {'data': error_msg}, room=bot_id)
        return jsonify({"error": "Workspace not found"}), 404

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

    socketio.emit('log', {
        'data': f'✅ Pre-flight checks passed'
    }, room=bot_id)
    
    socketio.emit('log', {
        'data': f'🚀 Executing: {startup_command}'
    }, room=bot_id)

    try:
        # Create subprocess with proper settings for real-time output
        process = subprocess.Popen(
            command_parts,
            cwd=bot_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # Line buffered
            universal_newlines=True,
            env=dict(os.environ, PYTHONUNBUFFERED="1")  # Force Python to be unbuffered
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
            'data': f'✅ Bot started successfully (PID: {process.pid})'
        }, room=bot_id)

        return jsonify({
            "success": True, 
            "pid": process.pid,
            "command": startup_command
        })

    except Exception as e:
        error_msg = f'💥 Failed to start bot: {str(e)}'
        socketio.emit('log', {'data': error_msg}, room=bot_id)
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
        # Install package in bot's virtual environment if it exists, otherwise globally
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
        socketio.run(app, host='0.0.0.0', port=30158, debug=True, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        print("\nShutting down...")
        cleanup_bot_processes()
