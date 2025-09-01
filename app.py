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

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY")
socketio = SocketIO(app)

running_bots = {} # {bot_id: threading.Event object}

# Discord OAuth2 settings
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = "http://localhost:5000/callback"
DISCORD_API_BASE_URL = "https://discord.com/api"
DISCORD_AUTHORIZATION_URL = f"https://discord.com/api/oauth2/authorize?client_id={DISCORD_CLIENT_ID}&redirect_uri={DISCORD_REDIRECT_URI}&response_type=code&scope=identify%20email"

# MongoDB setup
mongo_client = MongoClient(os.getenv("MONGO_URI"))
db = mongo_client.sycord

# Flask-Login setup
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

class User(UserMixin):
    def __init__(self, id, username, email):
        self.id = id
        self.username = username
        self.email = email

    @staticmethod
    def get(user_id):
        user_data = db.users.find_one({"_id": user_id})
        if user_data:
            return User(id=user_data["_id"], username=user_data["username"], email=user_data["email"])
        return None

    @staticmethod
    def create(id, username, email):
        user_data = {
            "_id": id,
            "username": username,
            "email": email,
            "bots": []
        }
        db.users.insert_one(user_data)

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
        return "Failed to retrieve access token.", 400

    headers = {
        "Authorization": f"Bearer {access_token}"
    }
    user_response = requests.get(f"{DISCORD_API_BASE_URL}/users/@me", headers=headers)
    user_data = user_response.json()

    user_id = user_data["id"]
    user = User.get(user_id)

    if user is None:
        User.create(id=user_id, username=user_data["username"], email=user_data["email"])
        user = User.get(user_id)

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
    bot_ids = user_data.get("bots", [])
    bots = list(db.bots.find({"_id": {"$in": bot_ids}}))
    return render_template("dashboard.html", user=current_user, bots=bots)

@app.route("/create_bot", methods=["POST"])
@login_required
def create_bot():
    bot_name = request.form.get("bot_name")
    startup_command = request.form.get("startup_command")

    new_bot = {
        "_id": ObjectId(),
        "name": bot_name,
        "owner_id": current_user.id,
        "status": "offline",
        "last_start_time": None,
        "uptime": 0,
        "startup_command": startup_command,
        "files": [], # Using a list of file documents
        "packages": []
    }
    bot_id = db.bots.insert_one(new_bot).inserted_id

    db.users.update_one(
        {"_id": current_user.id},
        {"$push": {"bots": bot_id}}
    )

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
    bot = db.bots.find_one({"_id": ObjectId(bot_id), "owner_id": current_user.id})
    if not bot:
        return "Unauthorized", 403

    file_tree = build_file_tree(bot.get('files', []))
    bot['files_tree'] = file_tree # Add the generated tree to the bot object

    return render_template("editor.html", bot=bot)

@app.route("/api/bot/<bot_id>/file", methods=["GET", "POST"])
@login_required
def file_content(bot_id):
    bot = db.bots.find_one({"_id": ObjectId(bot_id), "owner_id": current_user.id})
    if not bot:
        return jsonify({"error": "Unauthorized"}), 403

    path = request.args.get("path")
    if not path:
        return jsonify({"error": "File path is required"}), 400

    if request.method == "GET":
        file_doc = next((f for f in bot.get("files", []) if f["path"] == path), None)
        if file_doc and file_doc["type"] == "file":
            return jsonify({"content": file_doc.get("content", "")})
        return jsonify({"error": "File not found"}), 404

    if request.method == "POST":
        content = request.json.get("content")
        db.bots.update_one(
            {"_id": ObjectId(bot_id), "files.path": path},
            {"$set": {"files.$.content": content}}
        )
        return jsonify({"success": True})

@app.route("/api/bot/<bot_id>/files/create", methods=["POST"])
@login_required
def create_file(bot_id):
    path = request.json.get("path")
    type = request.json.get("type")
    if not path or not type:
        return jsonify({"error": "Path and type are required"}), 400

    # Check for duplicates
    existing = db.bots.find_one({"_id": ObjectId(bot_id), "files.path": path})
    if existing:
        return jsonify({"error": "File or folder with this path already exists"}), 400

    new_file_doc = {"path": path, "type": type}
    if type == "file":
        new_file_doc["content"] = ""

    db.bots.update_one(
        {"_id": ObjectId(bot_id)},
        {"$push": {"files": new_file_doc}}
    )
    return jsonify({"success": True})

@app.route("/api/bot/<bot_id>/files/delete", methods=["POST"])
@login_required
def delete_file(bot_id):
    path = request.json.get("path")
    if not path:
        return jsonify({"error": "Path is required"}), 400

    # If it's a folder, we need to delete all files inside it too
    result = db.bots.update_one(
        {"_id": ObjectId(bot_id)},
        {"$pull": {"files": {"path": {"$regex": f"^{path}"}}}}
    )

    if result.modified_count > 0:
        return jsonify({"success": True})
    return jsonify({"error": "File or folder not found"}), 404

def send_dummy_logs(bot_id, stop_event):
    """A background task that sends dummy log messages until stopped."""
    i = 0
    while not stop_event.is_set():
        socketio.emit('log', {'data': f'Log message {i} for bot {bot_id}'}, room=bot_id)
        i += 1
        time.sleep(2)
    socketio.emit('log', {'data': 'Bot process stopped.'}, room=bot_id)


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

    stop_event = threading.Event()
    running_bots[bot_id] = stop_event

    socketio.start_background_task(send_dummy_logs, bot_id, stop_event)

    db.bots.update_one({"_id": ObjectId(bot_id)}, {"$set": {"status": "online"}})

    return jsonify({"success": True})


@app.route("/api/bot/<bot_id>/stop", methods=["POST"])
@login_required
def stop_bot(bot_id):
    if bot_id not in running_bots:
        return jsonify({"error": "Bot is not running"}), 400

    stop_event = running_bots.pop(bot_id)
    stop_event.set()

    db.bots.update_one({"_id": ObjectId(bot_id)}, {"$set": {"status": "offline"}})

    return jsonify({"success": True})


@app.route("/api/bot/<bot_id>/packages/install", methods=["POST"])
@login_required
def install_package(bot_id):
    package_name = request.json.get("package_name")
    if not package_name:
        return jsonify({"error": "Package name is required"}), 400

    db.bots.update_one(
        {"_id": ObjectId(bot_id), "owner_id": current_user.id},
        {"$push": {"packages": package_name}}
    )
    return jsonify({"success": True})

@app.route("/api/bot/<bot_id>/packages/uninstall", methods=["POST"])
@login_required
def uninstall_package(bot_id):
    package_name = request.json.get("package_name")
    if not package_name:
        return jsonify({"error": "Package name is required"}), 400

    db.bots.update_one(
        {"_id": ObjectId(bot_id), "owner_id": current_user.id},
        {"$pull": {"packages": package_name}}
    )
    return jsonify({"success": True})


if __name__ == "__main__":
    socketio.run(app, debug=True)
