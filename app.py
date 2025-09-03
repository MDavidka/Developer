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
    bots = user_data.get("bots", [])
    return render_template("dashboard.html", user=current_user, bots=bots)

@app.route("/create_bot", methods=["POST"])
@login_required
def create_bot():
    bot_name = request.form.get("bot_name")
    startup_command = request.form.get("startup_command")
    bot_id = ObjectId()

    new_bot = {
        "_id": bot_id,
        "name": bot_name,
        "status": "offline",
        "last_start_time": None,
        "uptime": 0,
        "startup_command": startup_command,
        "files": [],
        "packages": []
    }

    db.users.update_one(
        {"_id": current_user.id},
        {"$push": {"bots": new_bot}}
    )

    # Create a directory for the bot
    bot_dir = os.path.join(BOT_WORKSPACES_PATH, str(bot_id))
    os.makedirs(bot_dir, exist_ok=True)

    return redirect(url_for("dashboard"))

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

@app.route("/editor/<bot_id>")
@login_required
def editor(bot_id):
    user_data = db.users.find_one({"_id": current_user.id})
    bot = next((b for b in user_data.get("bots", []) if str(b["_id"]) == bot_id), None)
    if not bot:
        return "Unauthorized", 403

    file_tree = build_file_tree(bot.get('files', []))
    bot['files_tree'] = file_tree

    return render_template("editor.html", bot=bot, user=current_user)

@app.route("/api/bot/<bot_id>/file", methods=["GET", "POST"])
@login_required
def file_content(bot_id):
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

        # Also update in the database for consistency if needed, though not strictly necessary if filesystem is the source of truth
        db.users.update_one(
            {"_id": current_user.id, "bots._id": ObjectId(bot_id), "bots.files.path": path},
            {"$set": {"bots.$[bot].files.$[file].content": content}},
            array_filters=[{"bot._id": ObjectId(bot_id)}, {"file.path": path}]
        )
        return jsonify({"success": True})

@app.route("/api/bot/<bot_id>/files/create", methods=["POST"])
@login_required
def create_file(bot_id):
    path = request.json.get("path")
    type = request.json.get("type")
    if not path or not type:
        return jsonify({"error": "Path and type are required"}), 400

    # Check for duplicates in the database
    existing = db.users.find_one({"_id": current_user.id, "bots._id": ObjectId(bot_id), "bots.files.path": path})
    if existing:
        return jsonify({"error": "File or folder with this path already exists"}), 400

    # Create on filesystem
    bot_dir = os.path.join(BOT_WORKSPACES_PATH, bot_id)
    full_path = os.path.join(bot_dir, path)

    if type == "folder":
        os.makedirs(full_path, exist_ok=True)
    else: # type == "file"
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w") as f:
            f.write("")

    # Add to database
    new_file_doc = {"path": path, "type": type}
    if type == "file":
        new_file_doc["content"] = ""

    db.users.update_one(
        {"_id": current_user.id, "bots._id": ObjectId(bot_id)},
        {"$push": {"bots.$.files": new_file_doc}}
    )
    return jsonify({"success": True})

@app.route("/api/bot/<bot_id>/files/delete", methods=["POST"])
@login_required
def delete_file(bot_id):
    path = request.json.get("path")
    if not path:
        return jsonify({"error": "Path is required"}), 400

    # Delete from filesystem
    bot_dir = os.path.join(BOT_WORKSPACES_PATH, bot_id)
    full_path = os.path.join(bot_dir, path)
    if os.path.isdir(full_path):
        import shutil
        shutil.rmtree(full_path)
    elif os.path.isfile(full_path):
        os.remove(full_path)

    # Delete from database
    result = db.users.update_one(
        {"_id": current_user.id, "bots._id": ObjectId(bot_id)},
        {"$pull": {"bots.$.files": {"path": {"$regex": f"^{path}"}}}}
    )

    if result.modified_count > 0:
        return jsonify({"success": True})
    return jsonify({"error": "File or folder not found"}), 404

def stream_logs(bot_id, process):
    """Stream logs from a subprocess to the client."""
    for line in iter(process.stdout.readline, ''):
        socketio.emit('log', {'data': line}, room=bot_id)
    for line in iter(process.stderr.readline, ''):
        socketio.emit('log', {'data': f'[ERROR] {line}'}, room=bot_id)

    # Process finished
    socketio.emit('log', {'data': 'Bot process stopped.'}, room=bot_id)
    db.users.update_one(
        {"bots._id": ObjectId(bot_id)},
        {"$set": {"bots.$.status": "offline"}}
    )
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


@app.route("/api/bot/<bot_id>/start", methods=["POST"])
@login_required
def start_bot(bot_id):
    if bot_id in running_bots:
        return jsonify({"error": "Bot is already running"}), 400

    user_data = db.users.find_one({"_id": current_user.id})
    bot = next((b for b in user_data.get("bots", []) if str(b["_id"]) == bot_id), None)
    if not bot:
        return jsonify({"error": "Bot not found"}), 404

    bot_dir = os.path.join(BOT_WORKSPACES_PATH, bot_id)
    startup_command = bot.get("startup_command")
    if not startup_command:
        return jsonify({"error": "Startup command not set"}), 400

    process = subprocess.Popen(
        startup_command.split(),
        cwd=bot_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    running_bots[bot_id] = process
    socketio.start_background_task(stream_logs, bot_id, process)

    db.users.update_one(
        {"bots._id": ObjectId(bot_id)},
        {"$set": {"bots.$.status": "online"}}
    )

    return jsonify({"success": True})


@app.route("/api/bot/<bot_id>/stop", methods=["POST"])
@login_required
def stop_bot(bot_id):
    if bot_id not in running_bots:
        return jsonify({"error": "Bot is not running"}), 400

    process = running_bots.pop(bot_id)
    process.terminate() # or process.kill()

    db.users.update_one(
        {"bots._id": ObjectId(bot_id)},
        {"$set": {"bots.$.status": "offline"}}
    )

    return jsonify({"success": True})


@app.route("/api/bot/<bot_id>/packages/install", methods=["POST"])
@login_required
def install_package(bot_id):
    package_name = request.json.get("package_name")
    if not package_name:
        return jsonify({"error": "Package name is required"}), 400

    # For simplicity, installing into the main env. A better solution would be bot-specific venvs.
    result = subprocess.run(["pip", "install", package_name], capture_output=True, text=True)
    if result.returncode != 0:
        return jsonify({"error": f"Failed to install package: {result.stderr}"}), 500

    db.users.update_one(
        {"_id": current_user.id, "bots._id": ObjectId(bot_id)},
        {"$push": {"bots.$.packages": package_name}}
    )
    return jsonify({"success": True, "message": result.stdout})

@app.route("/api/bot/<bot_id>/packages/uninstall", methods=["POST"])
@login_required
def uninstall_package(bot_id):
    package_name = request.json.get("package_name")
    if not package_name:
        return jsonify({"error": "Package name is required"}), 400

    result = subprocess.run(["pip", "uninstall", "-y", package_name], capture_output=True, text=True)
    if result.returncode != 0:
        return jsonify({"error": f"Failed to uninstall package: {result.stderr}"}), 500

    db.users.update_one(
        {"_id": current_user.id, "bots._id": ObjectId(bot_id)},
        {"$pull": {"bots.$.packages": package_name}}
    )
    return jsonify({"success": True, "message": result.stdout})


@app.route("/api/bot/<bot_id>/startup", methods=["POST"])
@login_required
def update_startup_command(bot_id):
    command = request.json.get("startup_command")
    if command is None:
        return jsonify({"error": "Startup command is required"}), 400

    db.users.update_one(
        {"_id": current_user.id, "bots._id": ObjectId(bot_id)},
        {"$set": {"bots.$.startup_command": command}}
    )
    return jsonify({"success": True})


if __name__ == "__main__":
    socketio.run(app, host='0.0.0.0', port=30158, debug=True, allow_unsafe_werkzeug=True)
