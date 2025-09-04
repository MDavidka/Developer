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

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY")
socketio = SocketIO(app)

BOT_WORKSPACES_PATH = "bot_workspaces"
if not os.path.exists(BOT_WORKSPACES_PATH):
    os.makedirs(BOT_WORKSPACES_PATH)

running_bots = {} # {bot_id: subprocess.Popen object}

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

    # NOTE: Using array index as an identifier is fragile.
    # It would be better to have a unique ID for each server/bot.
    bot_data = servers[server_index]

    # We need a unique and persistent ID for the workspace.
    # Using a combination of user ID and server name for now.
    # A dedicated botId in the server object would be best.
    bot_id = f"{current_user.id}_{bot_data.get('server_name', server_index)}"

    # The concept of 'files' needs to be re-evaluated.
    # For now, we'll assume it's still part of the server object.
    file_tree = build_file_tree(bot_data.get('files', []))
    bot_data['files_tree'] = file_tree
    bot_data['_id'] = bot_id # Pass a usable ID to the template for API calls

    return render_template("editor.html", bot=bot_data, user=current_user)

# Note: The 'bot_id' in these routes is a constructed ID for the workspace.
# It is not a real database ID. This is a temporary solution.

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
            with open(full_path, "r") as f:
                content = f.read()
            return jsonify({"content": content})
        except FileNotFoundError:
            return jsonify({"error": "File not found"}), 404

    if request.method == "POST":
        content = request.json.get("content")
        with open(full_path, "w") as f:
            f.write(content)
        return jsonify({"success": True})

@app.route("/api/server/<int:server_index>/files/create", methods=["POST"])
@login_required
def create_file(server_index):
    path = request.json.get("path")
    type = request.json.get("type")
    if not path or not type:
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

    if type == "folder":
        os.makedirs(full_path, exist_ok=True)
    else:
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w") as f:
            f.write("")

    return jsonify({"success": True})

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

    if os.path.isdir(full_path):
        import shutil
        shutil.rmtree(full_path)
    elif os.path.isfile(full_path):
        os.remove(full_path)
    else:
        return jsonify({"error": "File or folder not found"}), 404

    return jsonify({"success": True})

def stream_logs(bot_id, process):
    """Stream logs from a subprocess to the client."""
    socketio.emit('log', {'data': '[DEBUG] Log streaming thread started.'}, room=bot_id)
    def get_timestamp():
        return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    socketio.emit('log', {'data': f'[{get_timestamp()}] Bot process started.'}, room=bot_id)

    for line in iter(process.stdout.readline, ''):
        socketio.emit('log', {'data': f'[{get_timestamp()}] {line}'}, room=bot_id)
    for line in iter(process.stderr.readline, ''):
        socketio.emit('log', {'data': f'[{get_timestamp()}] [ERROR] {line}'}, room=bot_id)

    socketio.emit('log', {'data': f'[{get_timestamp()}] Bot process stopped.'}, room=bot_id)
    if bot_id in running_bots:
        del running_bots[bot_id]

@socketio.on('connect', namespace='/editor')
def editor_connect():
    emit('log', {'data': 'Connected to console...'})

@socketio.on('join', namespace='/editor')
def on_join(data):
    bot_id = data['bot_id']
    join_room(bot_id)
    emit('log', {'data': f'Console opened for bot {bot_id}'}, room=bot_id)

@app.route("/api/server/<int:server_index>/start", methods=["POST"])
@login_required
def start_bot(server_index):
    # This is a temporary, loud debug message.
    socketio.emit('log', {'data': '[DEBUG] start_bot function entered.'})

    user_data = db.users.find_one({"_id": current_user.id})
    socketio.emit('log', {'data': f'[DEBUG] Found user data for {current_user.id}.'})

    servers = user_data.get("servers", [])
    if server_index >= len(servers):
        socketio.emit('log', {'data': f'[ERROR] Server index {server_index} out of bounds.'})
        return jsonify({"error": "Server not found"}), 404

    bot_data = servers[server_index]
    bot_id = f"{current_user.id}_{bot_data.get('server_name', server_index)}"
    socketio.emit('log', {'data': f'[DEBUG] Constructed bot_id: {bot_id}.'})

    if bot_id in running_bots:
        socketio.emit('log', {'data': f'[ERROR] Bot {bot_id} is already in running_bots.'})
        return jsonify({"error": "Bot is already running"}), 400

    bot_dir = os.path.join(BOT_WORKSPACES_PATH, bot_id)
    startup_command = bot_data.get("startup_command", "python main.py")
    socketio.emit('log', {'data': f'[DEBUG] Bot directory: {bot_dir}'})
    socketio.emit('log', {'data': f'[DEBUG] Startup command: {startup_command}'})

    # Pre-flight checks and debugging
    socketio.emit('log', {'data': 'Initiating startup sequence...'}, room=bot_id)

    # Check 1: Workspace directory
    if not os.path.isdir(bot_dir):
        socketio.emit('log', {'data': f'[ERROR] Workspace directory not found: {bot_dir}'}, room=bot_id)
        return jsonify({"error": "Workspace not found"}), 404
    socketio.emit('log', {'data': '- Workspace directory found.'}, room=bot_id)

    # Check 2: Startup file
    command_parts = shlex.split(startup_command)
    startup_file = command_parts[1] if len(command_parts) > 1 else "main.py"
    if not os.path.isfile(os.path.join(bot_dir, startup_file)):
        socketio.emit('log', {'data': f'[ERROR] Startup file not found: {startup_file}'}, room=bot_id)
        return jsonify({"error": "Startup file not found"}), 404
    socketio.emit('log', {'data': '- Necessary files exist.'}, room=bot_id)

    socketio.emit('log', {'data': '- Connecting to machine...'}, room=bot_id)
    try:
        process = subprocess.Popen(
            command_parts,
            cwd=bot_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        running_bots[bot_id] = process
        socketio.emit('log', {'data': '[DEBUG] Subprocess created. Starting log streaming thread.'}, room=bot_id)
        socketio.start_background_task(stream_logs, bot_id, process)
    except Exception as e:
        socketio.emit('log', {'data': f'[ERROR] Failed to start bot process: {e}'}, room=bot_id)
        return jsonify({"error": "Failed to start bot process"}), 500

    socketio.emit('log', {'data': '[DEBUG] start_bot function finished successfully.'}, room=bot_id)
    return jsonify({"success": True})

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
        return jsonify({"error": "Bot is not running"}), 400

    process = running_bots.pop(bot_id)
    process.terminate()

    return jsonify({"success": True})

@app.route("/api/server/<int:server_index>/packages/install", methods=["POST"])
@login_required
def install_package(server_index):
    package_name = request.json.get("package_name")
    if not package_name:
        return jsonify({"error": "Package name is required"}), 400

    result = subprocess.run(["pip", "install", package_name], capture_output=True, text=True)
    if result.returncode != 0:
        return jsonify({"error": f"Failed to install package: {result.stderr}"}), 500

    return jsonify({"success": True, "message": result.stdout})

@app.route("/api/server/<int:server_index>/packages/uninstall", methods=["POST"])
@login_required
def uninstall_package(server_index):
    package_name = request.json.get("package_name")
    if not package_name:
        return jsonify({"error": "Package name is required"}), 400

    result = subprocess.run(["pip", "uninstall", "-y", package_name], capture_output=True, text=True)
    if result.returncode != 0:
        return jsonify({"error": f"Failed to uninstall package: {result.stderr}"}), 500

    return jsonify({"success": True, "message": result.stdout})

@app.route("/api/server/<int:server_index>/startup", methods=["POST"])
@login_required
def update_startup_command(server_index):
    command = request.json.get("startup_command")
    if command is None:
        return jsonify({"error": "Startup command is required"}), 400

    db.users.update_one(
        {"_id": current_user.id},
        {"$set": {f"servers.{server_index}.startup_command": command}}
    )
    return jsonify({"success": True})


if __name__ == "__main__":
    socketio.run(app, host='0.0.0.0', port=30158, debug=True, allow_unsafe_werkzeug=True)
