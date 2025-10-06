from gevent import monkey
monkey.patch_all()

import sqlite3
import logging
import os
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from flask import Flask, render_template, session, request, jsonify, redirect, url_for, g, send_from_directory
from flask_socketio import join_room, leave_room, SocketIO, emit
import uuid
import time
import secrets
import string
from dotenv import load_dotenv
from datetime import timedelta
import json
from collections import defaultdict
import firebase_admin
from firebase_admin import credentials, messaging
import requests
from requests.adapters import HTTPAdapter

load_dotenv()

firebase_session = requests.Session()
adapter = HTTPAdapter(pool_connections=100, pool_maxsize=100)
firebase_session.mount('https://', adapter)

try:
    cred = credentials.Certificate("serviceAccountKey.json") 
    firebase_admin.initialize_app(cred, {'http_client': firebase_session})
    logging.info("Firebase Admin SDK initialized successfully with custom HTTP session.")
except Exception as e:
    logging.error(f"Failed to initialize Firebase Admin SDK: {e}")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logging.getLogger('socketio').setLevel(logging.WARNING)
logging.getLogger('engineio').setLevel(logging.WARNING)

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY') or 'a-very-secret-key'
app.permanent_session_lifetime = timedelta(days=30)

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent', ping_timeout=60, ping_interval=25, logger=False, engineio_logger=False)

# --- NEW: Robust Presence Tracking Globals ---
sid_to_user = {}  # Maps a connection SID to a username
user_to_sids = defaultdict(set) # Maps a username to a set of their connection SIDs
sid_to_room = {}  # Maps a connection SID to the room it's in

dev_mode_users = set()

DB_FILE = "chat1.db"

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_FILE, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db

@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db:
        db.close()

def init_db():
    # This function is now simplified, without the notification prefs table
    try:
        with app.app_context():
            db = get_db()
            c = db.cursor()
            
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
            if not c.fetchone():
                with app.open_resource('schema.sql', mode='r') as f:
                    db.executescript(f.read())
                db.commit()
                logging.info("Initialized database from schema.sql")

            c.execute("PRAGMA table_info(users)")
            columns = [row['name'] for row in c.fetchall()]
            
            if 'plain_password' not in columns:
                db.execute("ALTER TABLE users ADD COLUMN plain_password TEXT")
            if 'user_code' not in columns:
                db.execute("ALTER TABLE users ADD COLUMN user_code TEXT")
            if 'hidden' not in columns:
                db.execute("ALTER TABLE users ADD COLUMN hidden INTEGER DEFAULT 0")
            
            c.execute("PRAGMA table_info(push_subscriptions)")
            sub_columns = [row['name'] for row in c.fetchall()]
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_room_notification_prefs'")
            if not c.fetchone():
                db.execute("""
                    CREATE TABLE user_room_notification_prefs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        username TEXT NOT NULL,
                        room_unique_id TEXT NOT NULL,
                        notifications_enabled INTEGER NOT NULL DEFAULT 1,
                        UNIQUE(username, room_unique_id),
                        FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE,
                        FOREIGN KEY (room_unique_id) REFERENCES rooms(unique_id) ON DELETE CASCADE
                    )
                """)
                logging.info("Created user_room_notification_prefs table")

            # Also add the global preference column to users table
            c.execute("PRAGMA table_info(users)")
            columns = [row['name'] for row in c.fetchall()]
            if 'notifications_enabled' not in columns:
                db.execute("ALTER TABLE users ADD COLUMN notifications_enabled INTEGER DEFAULT 1")
            if 'fcm_token' not in sub_columns:
                c.execute("DROP TABLE IF EXISTS push_subscriptions")
                db.execute("""
                    CREATE TABLE push_subscriptions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        username TEXT NOT NULL,
                        fcm_token TEXT NOT NULL UNIQUE,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
                    )
                """)
            
            db.commit()

    except Exception as e:
        logging.error(f"Database initialization or migration failed: {e}")
init_db()

def save_message(room_unique_id, user, message):
    try:
        db = get_db()
        db.execute("INSERT INTO messages (room_unique_id, user, message, timestamp) VALUES (?, ?, ?, CURRENT_TIMESTAMP)", (room_unique_id, user, message))
        db.commit()
        return True
    except sqlite3.Error as e:
        logging.error(f"DATABASE SAVE FAILED in room {room_unique_id} for user {user}: {e}")
        db.rollback()
        return False

def get_room_details(unique_id):
    c = get_db().cursor()
    c.execute("SELECT * FROM rooms WHERE unique_id = ?", (unique_id,))
    return c.fetchone()

def add_user_to_room_members(room_unique_id, username):
    db = get_db()
    db.execute("INSERT OR IGNORE INTO room_members (room_unique_id, username, last_seen) VALUES (?, ?, CURRENT_TIMESTAMP)", (room_unique_id, username))
    db.commit()

def get_room_members(room_unique_id):
    c = get_db().cursor()
    c.execute("SELECT username FROM room_members WHERE room_unique_id = ?", (room_unique_id,))
    return [row['username'] for row in c.fetchall()]

def get_all_users():
    c = get_db().cursor()
    c.execute("SELECT id, username, is_verified FROM users WHERE username_lower != 'pratotasrik72'")
    return c.fetchall()

def get_all_rooms_with_members():
    db = get_db()
    rooms = db.execute("SELECT unique_id, name, description, password, hidden FROM rooms").fetchall()
    rooms_data = []
    for room in rooms:
        room_dict = dict(room)
        room_dict['members'] = get_room_members(room['unique_id'])
        banned = db.execute("SELECT username FROM banned_users WHERE room_unique_id = ?", (room['unique_id'],)).fetchall()
        room_dict['banned_users'] = [row['username'] for row in banned]
        rooms_data.append(room_dict)
    return rooms_data

def get_user_profile(username):
    c = get_db().cursor()
    c.execute("SELECT username, bio, is_verified FROM users WHERE username_lower = ?", (username.lower(),))
    return c.fetchone()

@app.before_request
def sync_dev_mode_state():
    admin_username_lower = 'pratotasrik72'
    if session.get('username', '').lower() == admin_username_lower:
        if session.get('dev_mode', False):
            dev_mode_users.add(admin_username_lower)
        else:
            dev_mode_users.discard(admin_username_lower)

@app.context_processor
def inject_impersonation_status():
    return dict(is_impersonating=session.get('impersonating_from') is not None)

@app.route("/")
def main_redirect():
    if "username" in session:
        return redirect(url_for("index"))
    return redirect(url_for("login"))

@app.route("/control_panel", methods=["GET", "POST"])
def control_panel():
    admin_username_lower = 'pratotasrik72'
    if session.get('username', '').lower() != admin_username_lower:
        return "Access Denied", 403

    CONTROL_PANEL_PASSWORD = "kuttashopno"
    if not session.get('control_access'):
        if request.method == "POST" and request.form.get('password') == CONTROL_PANEL_PASSWORD:
            session['control_access'] = True
        else:
            error = "Incorrect password." if request.method == "POST" else None
            return render_template("control.html", access_granted=False, error=error)

    if request.method == "POST" and request.form.get("toggle_dev_mode"):
        session['dev_mode'] = not session.get('dev_mode', False)
        return redirect(url_for('control_panel'))
    
    all_users = get_all_users()
    all_rooms = get_all_rooms_with_members()
    user_profile = get_user_profile(session['username'])
    
    return render_template("control.html", 
                           access_granted=True, 
                           users=all_users, 
                           rooms=all_rooms, 
                           user_profile=user_profile,
                           session=session)

@app.route("/rooms")
def index():
    if "username" not in session:
        return redirect(url_for("login"))
    is_admin = session.get('username', '').lower() == 'pratotasrik72'
    user_profile = get_db().execute("SELECT username, bio, is_verified FROM users WHERE username_lower = ?", (session['username'].lower(),)).fetchone()
    return render_template("index.html", is_admin=is_admin, user_profile=user_profile)

@app.route("/user/<username>")
def user_profile_page(username):
    if "username" not in session:
        return redirect(url_for("login"))
    
    profile_data = get_user_profile(username)
    if not profile_data:
        return "User not found", 404
    
    return render_template("user_profile.html", profile=profile_data)

@app.route('/sw.js')
def service_worker():
    response = send_from_directory('static', 'sw.js')
    response.headers['Content-Type'] = 'application/javascript'
    response.headers['Service-Worker-Allowed'] = '/'
    return response
@app.route('/firebase-messaging-sw.js')
def firebase_messaging_sw_js():
    response = send_from_directory('static', 'sw.js')
    response.headers['Content-Type'] = 'application/javascript'
    response.headers['Service-Worker-Allowed'] = '/'
    return response

@app.route('/manifest.json')
def manifest():
    return send_from_directory('static', 'manifest.json', mimetype='application/manifest+json')

# NEW: Route for assetlinks.json for WebAPK/TWA support (Fixes Bug 3)
@app.route('/.well-known/assetlinks.json')
def assetlinks():
    return send_from_directory(os.path.join(app.root_path, 'static', '.well-known'),
                               'assetlinks.json', mimetype='application/json')

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static', 'icons'),
                               'icon-192.png', mimetype='image/png')

@app.route("/download")
def download_pwa():
    return render_template("download.html")

@app.route("/chat/<unique_id>")
def chat(unique_id):
    if "username" not in session:
        return redirect(url_for("login"))
    
    room = get_room_details(unique_id)
    if not room:
        logging.warning(f"Invalid room access: {unique_id}")
        return redirect(url_for("index"))
    
    try:
        add_user_to_room_members(unique_id, session['username'])
        
        messages = get_db().execute("""
            SELECT id, user, message, timestamp 
            FROM messages WHERE room_unique_id = ? 
            ORDER BY timestamp DESC LIMIT 50
        """, (unique_id,)).fetchall()
        messages = [dict(row) for row in reversed(messages)]
        
        return render_template("chat_v2.html", 
                               room=dict(room), 
                               messages=messages, 
                               username=session['username'])
    except Exception as e:
        logging.error(f"Chat render error for {unique_id}: {e}", exc_info=True)
        return render_template("error.html", error="Could not load chat room.")

@app.route("/login", methods=["GET", "POST"])
def login():
    if "username" in session:
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        if not username or not password:
            return render_template("account.html", page="login", error="Username and password are required.")
        if len(password) < 6:
            return render_template("account.html", page="login", error="Password must be at least 6 characters.")

        db = get_db()
        user = db.execute("SELECT username, password FROM users WHERE username_lower = ?", (username.lower(),)).fetchone()

        if user:
            if check_password_hash(user['password'], password):
                session["username"] = user['username']
                session.permanent = True
                return redirect(url_for("index"))
            else:
                return render_template("account.html", page="login", error="Incorrect password for this username.")
        else:
            try:
                hashed = generate_password_hash(password)
                db.execute("INSERT INTO users (username, username_lower, password, plain_password) VALUES (?, ?, ?, ?)",
                           (username, username.lower(), hashed, password))
                db.commit()
                session["username"] = username
                session.permanent = True
                return redirect(url_for("index"))
            except sqlite3.IntegrityError:
                return render_template("account.html", page="login", error="An error occurred. Please try again.")

    return render_template("account.html", page="login")

@app.route("/register", methods=["GET", "POST"])
def register():
    if "username" in session:
        return redirect(url_for("index"))
    if request.method == "POST":
        username, password = request.form.get("username"), request.form.get("password")
        if len(password) < 6:
            return render_template("account.html", page="register", error="Password too short.")
        
        db = get_db()
        c = db.cursor()
        c.execute("SELECT 1 FROM users WHERE username_lower = ?", (username.lower(),))
        if c.fetchone():
            return render_template("account.html", page="register", error="Username exists.")
        
        hashed = generate_password_hash(password)
        c.execute("INSERT INTO users (username, username_lower, password, plain_password) VALUES (?, ?, ?, ?)", 
                  (username, username.lower(), hashed, password))
        db.commit()
        session["username"] = username
        session.permanent = True
        return redirect(url_for("index"))
    return render_template("account.html", page="register")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# --- FIX: API endpoint updated to use the new presence system ---
@app.route('/api/rooms/<unique_id>/users')
def api_get_room_users(unique_id):
    if 'username' not in session:
        return jsonify([]), 401

    try:
        db = get_db()
        c = db.cursor()
        c.execute("""
            SELECT u.username, u.is_verified
            FROM users u
            JOIN room_members rm ON u.username = rm.username
            WHERE rm.room_unique_id = ?
            ORDER BY u.username
        """, (unique_id,))
        
        members_from_db = [dict(row) for row in c.fetchall()]
        
        # Determine active users in this room using the robust presence system
        active_users_in_this_room = set()
        for user, sids in user_to_sids.items():
            for sid in sids:
                if sid_to_room.get(sid) == unique_id:
                    active_users_in_this_room.add(user)
                    break

        enhanced_members = []
        for member in members_from_db:
            username = member['username']
            member['active_in_room'] = username in active_users_in_this_room
            enhanced_members.append(member)
            
        return jsonify(enhanced_members)
        
    except Exception as e:
        logging.error(f"API get room users error for {unique_id}: {e}")
        return jsonify([]), 500

@app.route("/api/create_room", methods=["POST"])
def api_create_room():
    if "username" not in session:
        return jsonify({"error": "Login required"}), 401
    
    data = request.json
    name = data.get('name')
    if not name or len(name) > 50:
        return jsonify({"error": "Invalid room name"}), 400
        
    password = data.get('password', '')
    hidden = bool(data.get('hidden', False))
    
    db = get_db()
    
    existing_room = db.execute("SELECT 1 FROM rooms WHERE LOWER(name) = ?", (name.lower(),)).fetchone()
    if existing_room:
        return jsonify({"error": "A room with this name already exists."}), 409

    unique_id = str(uuid.uuid4().hex)[:6]
    
    db.execute("INSERT INTO rooms (unique_id, name, password, description, hidden) VALUES (?, ?, ?, ?, ?)",
               (unique_id, name, password, f"Welcome to {name}!", hidden))
    db.commit()
    
    add_user_to_room_members(unique_id, session['username'])
    
    return jsonify({"success": True, "unique_id": unique_id})

@app.route('/api/rooms')
def api_rooms():
    is_dev_mode = session.get('dev_mode', False)
    is_admin = session.get('username', '').lower() == 'pratotasrik72'
    
    query = "SELECT unique_id, name, password FROM rooms"
    if not (is_admin and is_dev_mode):
        query += " WHERE hidden = 0"
    query += " ORDER BY name ASC"
    
    c = get_db().cursor()
    c.execute(query)
    rooms = [{"unique_id": row["unique_id"], "name": row["name"], "protected": bool(row["password"])} for row in c.fetchall()]
    return jsonify(rooms)

@app.route('/api/join_room', methods=['POST'])
def api_join_room():
    if "username" not in session:
        return jsonify({"error": "Login required"}), 401
    
    data = request.json
    unique_id = data.get('unique_id')
    password = data.get('password', '')
    username = session['username']
    
    room = get_room_details(unique_id)
    if not room:
        return jsonify({"error": "Room not found"}), 404

    db = get_db()
    is_banned = db.execute("SELECT 1 FROM banned_users WHERE room_unique_id = ? AND username = ?", 
                           (unique_id, username)).fetchone()
    if is_banned:
        return jsonify({"error": "You are banned from this room."}), 403

    is_dev_mode = session.get('dev_mode', False)
    is_admin = username.lower() == 'pratotasrik72'
    if not (is_dev_mode and is_admin) and room['password'] and room['password'] != password:
        return jsonify({"error": "Wrong password"}), 403
    
    db.execute('INSERT OR IGNORE INTO room_members (room_unique_id, username, last_seen) VALUES (?, ?, CURRENT_TIMESTAMP)', 
               (unique_id, username))
    db.commit()
    
    return jsonify({"success": True})

@app.route('/api/admin/get_user_details', methods=['POST'])
def admin_get_user_details():
    if session.get('username', '').lower() != 'pratotasrik72': 
        return jsonify(success=False), 403
    username = request.json.get('username')
    user = get_db().execute("SELECT username, password, bio, plain_password FROM users WHERE username = ?", (username,)).fetchone()
    if user:
        details = dict(user)
        details['password'] = details['password'][:10] + '...'
        return jsonify(success=True, details=details)
    return jsonify(success=False, error="User not found"), 404

@app.route('/api/admin/toggle_verify', methods=['POST'])
def admin_toggle_verify():
    if session.get('username', '').lower() != 'pratotasrik72': return jsonify(success=False), 403
    username = request.json.get('username')
    db = get_db()
    current_status = db.execute("SELECT is_verified FROM users WHERE username = ?", (username,)).fetchone()
    if not current_status: return jsonify(success=False, error="User not found"), 404
    new_status = 0 if current_status['is_verified'] else 1
    db.execute("UPDATE users SET is_verified = ? WHERE username = ?", (new_status, username))
    db.commit()
    return jsonify(success=True, is_verified=bool(new_status))

@app.route('/api/admin/delete_user', methods=['POST'])
def admin_delete_user():
    if session.get('username', '').lower() != 'pratotasrik72': return jsonify(success=False), 403
    username = request.json.get('username')
    db = get_db()
    db.execute("DELETE FROM users WHERE username = ?", (username,))
    db.execute("DELETE FROM messages WHERE user = ?", (username,))
    db.execute("DELETE FROM room_members WHERE username = ?", (username,))
    db.commit()
    return jsonify(success=True)

@app.route('/api/admin/login_as', methods=['POST'])
def admin_login_as():
    if session.get('username', '').lower() != 'pratotasrik72': return jsonify(success=False), 403
    admin_username = session['username']
    username_to_impersonate = request.json.get('username')
    session.clear()
    session['username'] = username_to_impersonate
    session['impersonating_from'] = admin_username
    return jsonify(success=True)

@app.route('/admin/return_to_admin')
def return_to_admin():
    admin_username = session.get('impersonating_from')
    if not admin_username:
        return redirect(url_for('index'))
    
    session.clear()
    session['username'] = admin_username
    session['control_access'] = True
    return redirect(url_for('control_panel'))

@app.route('/api/admin/update_room', methods=['POST'])
def admin_update_room():
    if session.get('username', '').lower() != 'pratotasrik72': return jsonify(success=False), 403
    data = request.json
    db = get_db()
    db.execute("UPDATE rooms SET description = ?, password = ? WHERE unique_id = ?", 
               (data.get('description'), data.get('password'), data.get('room_id')))
    db.commit()
    return jsonify(success=True)

@app.route('/api/admin/ban_user', methods=['POST'])
def admin_ban_user():
    if session.get('username', '').lower() != 'pratotasrik72': return jsonify(success=False), 403
    data = request.json
    db = get_db()
    db.execute("INSERT OR IGNORE INTO banned_users (room_unique_id, username) VALUES (?, ?)", (data.get('room_id'), data.get('username')))
    db.execute("DELETE FROM room_members WHERE room_unique_id = ? AND username = ?", (data.get('room_id'), data.get('username')))
    db.commit()
    return jsonify(success=True)

@app.route('/api/admin/delete_room', methods=['POST'])
def admin_delete_room():
    if session.get('username', '').lower() != 'pratotasrik72': return jsonify(success=False), 403
    room_id = request.json.get('room_id')
    db = get_db()
    db.execute("DELETE FROM rooms WHERE unique_id = ?", (room_id,))
    db.execute("DELETE FROM messages WHERE room_unique_id = ?", (room_id,))
    db.execute("DELETE FROM room_members WHERE room_unique_id = ?", (room_id,))
    db.execute("DELETE FROM banned_users WHERE room_unique_id = ?", (room_id,))
    db.commit()
    return jsonify(success=True)

@app.route('/api/admin/toggle_room_visibility', methods=['POST'])
def admin_toggle_room_visibility():
    if session.get('username', '').lower() != 'pratotasrik72': return jsonify(success=False), 403
    room_id = request.json.get('room_id')
    db = get_db()
    current = db.execute("SELECT hidden FROM rooms WHERE unique_id = ?", (room_id,)).fetchone()
    if not current: return jsonify(success=False, error="Room not found"), 404
    new_hidden_status = 0 if current['hidden'] else 1
    db.execute("UPDATE rooms SET hidden = ? WHERE unique_id = ?", (new_hidden_status, room_id))
    db.commit()
    return jsonify(success=True, is_hidden=bool(new_hidden_status))

@app.route('/api/admin/unban_user', methods=['POST'])
def admin_unban_user():
    if session.get('username', '').lower() != 'pratotasrik72': return jsonify(success=False), 403
    data = request.json
    db = get_db()
    db.execute("DELETE FROM banned_users WHERE room_unique_id = ? AND username = ?", (data.get('room_id'), data.get('username')))
    db.commit()
    return jsonify(success=True)

@app.route('/api/admin/reset_password', methods=['POST'])
def admin_reset_password():
    if session.get('username', '').lower() != 'pratotasrik72': 
        return jsonify(success=False, error="Unauthorized"), 403
    
    username = request.json.get('username')
    if not username:
        return jsonify(success=False, error="Username required"), 400

    alphabet = string.ascii_letters + string.digits
    new_password = ''.join(secrets.choice(alphabet) for i in range(8))
    
    db = get_db()
    user = db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
    if not user:
        return jsonify(success=False, error="User not found"), 404

    hashed_password = generate_password_hash(new_password)
    db.execute("UPDATE users SET password = ? WHERE username = ?", (hashed_password, username))
    db.commit()
    
    return jsonify(success=True, new_password=new_password)

@app.route('/api/update_bio', methods=['POST'])
def api_update_bio():
    if "username" not in session:
        return jsonify({"success": False, "error": "Not logged in"}), 401
    
    new_bio = request.json.get('bio', '').strip()
    if len(new_bio) > 150:
        return jsonify({"success": False, "error": "Bio cannot exceed 150 characters."}), 400

    db = get_db()
    db.execute("UPDATE users SET bio = ? WHERE username = ?", (new_bio, session['username']))
    db.commit()
    return jsonify({"success": True, "message": "Bio updated!"})

@app.route('/api/change_username', methods=['POST'])
def api_change_username():
    if "username" not in session:
        return jsonify({"success": False, "error": "Not logged in"}), 401

    new_username = request.json.get('new_username', '').strip()
    password = request.json.get('password')
    old_username = session['username']

    if not new_username or not password or len(new_username) < 3:
        return jsonify({"success": False, "error": "Invalid input."}), 400

    db = get_db()
    user = db.execute("SELECT password FROM users WHERE username = ?", (old_username,)).fetchone()

    if not user or not check_password_hash(user['password'], password):
        return jsonify({"success": False, "error": "Password is incorrect."}), 403

    existing_user = db.execute("SELECT 1 FROM users WHERE username_lower = ? AND username_lower != ?", 
                               (new_username.lower(), old_username.lower())).fetchone()
    if existing_user:
        return jsonify({"success": False, "error": "Username is already taken."}), 409

    db.execute("UPDATE users SET username = ?, username_lower = ? WHERE username = ?", (new_username, new_username.lower(), old_username))
    db.execute("UPDATE messages SET user = ? WHERE user = ?", (new_username, old_username))
    db.execute("UPDATE room_members SET username = ? WHERE username = ?", (new_username, old_username))
    db.commit()

    session['username'] = new_username
    socketio.emit('username_changed', {'old_username': old_username, 'new_username': new_username}, broadcast=True)
    return jsonify({"success": True, "message": "Username changed successfully!"})

@app.route('/api/user_rooms')
def api_user_rooms():
    if "username" not in session:
        return jsonify({"error": "Not logged in"}), 401
    try:
        db = get_db()
        c = db.cursor()
        c.execute("""
            SELECT r.unique_id, r.name, r.password 
            FROM rooms r
            JOIN room_members rm ON r.unique_id = rm.room_unique_id
            WHERE rm.username = ?
        """, (session['username'],))
        rooms = [{"unique_id": row["unique_id"], "name": row["name"], "protected": bool(row["password"])} for row in c.fetchall()]
        return jsonify(rooms)
    except Exception as e:
        logging.error(f"API get user rooms error: {e}")
        return jsonify([]), 500

@app.route('/api/change_password', methods=['POST'])
def api_change_password():
    if "username" not in session:
        return jsonify({"success": False, "error": "Not logged in"}), 401
    
    data = request.json
    current_password = data.get('current_password')
    new_password = data.get('new_password')
    username = session['username']

    if not current_password or not new_password or len(new_password) < 6:
        return jsonify({"success": False, "error": "Invalid input."}), 400

    db = get_db()
    user = db.execute("SELECT password FROM users WHERE username = ?", (username,)).fetchone()

    if not user or not check_password_hash(user['password'], current_password):
        return jsonify({"success": False, "error": "Current password is incorrect."}), 403

    db.execute("UPDATE users SET password = ? WHERE username = ?", (generate_password_hash(new_password), username))
    db.commit()
    return jsonify({"success": True, "message": "Password updated successfully!"})

@app.route('/api/delete_account', methods=['POST'])
def api_delete_account():
    if "username" not in session:
        return jsonify({"success": False, "error": "Not logged in"}), 401

    password = request.json.get('password')
    username = session['username']
    db = get_db()
    user = db.execute("SELECT password FROM users WHERE username = ?", (username,)).fetchone()

    if not user or not check_password_hash(user['password'], password):
        return jsonify({"success": False, "error": "Password is incorrect."}), 403

    db.execute("DELETE FROM users WHERE username = ?", (username,))
    db.execute("DELETE FROM messages WHERE user = ?", (username,))
    db.execute("DELETE FROM room_members WHERE username = ?", (username,))
    db.commit()
    session.clear()
    return jsonify({"success": True, "message": "Account deleted successfully."})

@app.route('/api/user_code', methods=['GET'])
def api_user_code():
    if 'username' not in session:
        return jsonify({'error': 'Login required'}), 401
    try:
        db = get_db()
        c = db.cursor()
        username_lower = session['username'].lower()
        c.execute("SELECT user_code FROM users WHERE username_lower = ?", (username_lower,))
        row = c.fetchone()
        if row and row['user_code']:
            user_code = row['user_code']
        else:
            user_code = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
            db.execute("UPDATE users SET user_code = ? WHERE username_lower = ?", (user_code, username_lower))
            db.commit()
        return jsonify({'user_code': user_code})
    except Exception as e:
        logging.error(f"User code error: {e}")
        return jsonify({'error': 'Internal error'}), 500

@app.route('/api/hide_profile', methods=['POST'])
def api_hide_profile():
    if 'username' not in session:
        return jsonify({'success': False, 'error': 'Login required'}), 401
    try:
        data = request.json
        hidden = int(data.get('hidden', 0))
        db = get_db()
        db.execute("UPDATE users SET hidden = ? WHERE username_lower = ?", (hidden, session['username'].lower()))
        db.commit()
        return jsonify({'success': True, 'hidden': bool(hidden)})
    except Exception as e:
        logging.error(f"Hide profile error: {e}")
        return jsonify({'success': False, 'error': 'Internal error'}), 500

@app.route('/api/chat/<room_id>/older')
def api_get_older_messages(room_id):
    if 'username' not in session:
        return jsonify([]), 401
    
    before_timestamp = request.args.get('before')
    if not before_timestamp:
        return jsonify([]), 400

    try:
        db = get_db()
        c = db.cursor()
        c.execute("""
            SELECT id, user, message, timestamp
            FROM messages
            WHERE room_unique_id = ? AND timestamp < ?
            ORDER BY timestamp DESC
            LIMIT 20
        """, (room_id, before_timestamp))
        
        messages = [dict(row) for row in c.fetchall()]
        return jsonify(messages)
    except Exception as e:
        logging.error(f"API get older messages error: {e}")
        return jsonify([]), 500

# NEW: API to subscribe to push notifications
@app.route('/api/subscribe_push', methods=['POST'])
def subscribe_push():
    if 'username' not in session:
        return jsonify({'error': 'Login required'}), 401
    
    data = request.json
    token = data.get('token') # We now expect an FCM token

    if not token:
        return jsonify({'error': 'Missing FCM token'}), 400

    try:
        db = get_db()
        db.execute("""
            INSERT OR REPLACE INTO push_subscriptions (username, fcm_token) 
            VALUES (?, ?)
        """, (session['username'], token))
        db.commit()
        logging.info(f"FCM token saved for {session['username']}")
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Error saving FCM token for {session['username']}: {e}")
        return jsonify({'success': False, 'error': 'Server error'}), 500

@app.route('/api/unsubscribe_push', methods=['POST'])
def unsubscribe_push():
    if 'username' not in session:
        return jsonify({'error': 'Login required'}), 401
    
    data = request.json
    token = data.get('token') # We now expect an FCM token

    if not token:
        return jsonify({'error': 'Missing FCM token'}), 400

    try:
        db = get_db()
        db.execute("DELETE FROM push_subscriptions WHERE username = ? AND fcm_token = ?", 
                   (session['username'], token))
        db.commit()
        logging.info(f"FCM token removed for {session['username']}")
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Error removing FCM token for {session['username']}: {e}")
        return jsonify({'success': False, 'error': 'Server error'}), 500
@app.route('/api/clear_my_push_token', methods=['POST'])
def api_clear_my_push_token():
    """Clears the current user's FCM token, forcing re-subscription."""
    if 'username' not in session:
        return jsonify({'error': 'Login required'}), 401

    try:
        db = get_db()
        # Delete all push tokens for the current user
        db.execute("DELETE FROM push_subscriptions WHERE username = ?", (session['username'],))
        db.commit()
        logging.info(f"Cleared all FCM tokens for user: {session['username']}")
        return jsonify({'success': True, 'message': 'Push token cleared. Please reload the page to enable notifications again.'})
    except Exception as e:
        logging.error(f"Error clearing push tokens for {session['username']}: {e}")
        return jsonify({'success': False, 'error': 'Server error'}), 500
@app.errorhandler(Exception)
def handle_exception(e):
    logging.error(f"Unhandled exception on {request.path}:", exc_info=e)
    
    if request.path.startswith('/api/'):
        return jsonify(error="An internal server error occurred."), 500
    
    return render_template("error.html", error=str(e)), 500

@app.route('/api/notification_preferences', methods=['GET'])
def get_notification_preferences():
    if 'username' not in session:
        return jsonify({'error': 'Login required'}), 401
    
    try:
        db = get_db()
        # Get user's global notification preference
        c = db.execute("SELECT notifications_enabled FROM users WHERE username = ?", 
                      (session['username'],))
        user_pref = c.fetchone()
        
        # Get room-specific preferences
        c = db.execute("""
            SELECT room_unique_id, notifications_enabled 
            FROM user_room_notification_prefs 
            WHERE username = ?
        """, (session['username'],))
        room_prefs = {row['room_unique_id']: bool(row['notifications_enabled']) for row in c.fetchall()}
        
        return jsonify({
            'global': bool(user_pref['notifications_enabled']) if user_pref else True,
            'rooms': room_prefs
        })
    except Exception as e:
        logging.error(f"Error getting notification preferences: {e}")
        return jsonify({'error': 'Server error'}), 500

@app.route('/api/notification_preferences', methods=['POST'])
def update_notification_preferences():
    if 'username' not in session:
        return jsonify({'error': 'Login required'}), 401
    
    data = request.json
    room_id = data.get('room_id')
    enabled = data.get('enabled', True)
    
    try:
        db = get_db()
        if room_id:
            # Update room-specific preference
            db.execute("""
                INSERT INTO user_room_notification_prefs (username, room_unique_id, notifications_enabled)
                VALUES (?, ?, ?)
                ON CONFLICT(username, room_unique_id) DO UPDATE SET
                notifications_enabled = excluded.notifications_enabled
            """, (session['username'], room_id, int(enabled)))
        else:
            # Update global preference
            db.execute("UPDATE users SET notifications_enabled = ? WHERE username = ?",
                      (int(enabled), session['username']))
        
        db.commit()
        return jsonify({'success': True, 'enabled': bool(enabled)})
    except Exception as e:
        logging.error(f"Error updating notification preferences: {e}")
        return jsonify({'error': 'Server error'}), 500
# --- Socket.IO Event Handlers (FIXED: Using session for authentication) ---

@socketio.on('connect')
def on_connect():
    # Authenticate and identify the user using their Flask session on connection.
    if 'username' not in session:
        logging.warning(f"Anonymous SID {request.sid} connected. No user session.")
        return

    username = session['username']
    sid_to_user[request.sid] = username
    user_to_sids[username].add(request.sid)
    logging.info(f"Connect & Identified: {username} (SID: {request.sid}). Total connections for user: {len(user_to_sids[username])}")

@socketio.on('disconnect')
def on_disconnect():
    # This logic remains the same, but is now more reliable.
    username = sid_to_user.pop(request.sid, None)
    room_id = sid_to_room.pop(request.sid, None)

    if username:
        user_to_sids[username].discard(request.sid)
        if not user_to_sids[username]:
            del user_to_sids[username]
        
        logging.info(f"Disconnect: {username} (SID: {request.sid}). Remaining connections: {len(user_to_sids.get(username, []))}")
        
        if room_id:
            socketio.emit("user_list_updated", {}, room=room_id)

@socketio.on("join")
def on_join(data):
    with app.app_context():
        # Get the username securely from our session-populated map.
        username = sid_to_user.get(request.sid)
        room_id = data.get('room')
        
        if not username or not room_id:
            logging.warning(f"Join failed: Unidentified SID or no room_id (SID: {request.sid})")
            return
        
        previous_room = sid_to_room.get(request.sid)
        if previous_room:
            leave_room(previous_room)

        join_room(room_id)
        sid_to_room[request.sid] = room_id
        
        add_user_to_room_members(room_id, username)
        
        logging.info(f"Join: {username} joined {room_id} (SID: {request.sid})")
        emit("user_list_updated", {}, room=room_id)

@socketio.on("send_message")
def handle_message(data):
    with app.app_context():
        # Get the username securely from our session-populated map.
        username = sid_to_user.get(request.sid)
        
        # This check is now robust. It fails if the user isn't logged in or the connection is bad.
        if not username:
            logging.warning(f"Rejected send: Message from unidentified SID {request.sid}")
            return

        # As a sanity check, we can still ensure the payload matches the identified user.
        if username != data.get('user'):
            logging.warning(f"Rejected send: Payload user '{data.get('user')}' mismatches identified user '{username}' for SID {request.sid}")
            return

        room_id = data.get('room')
        message = data.get('message', '').strip()
        
        if not all([room_id, message]):
            return

        if save_message(room_id, username, message):
            data['user'] = username
            emit("receive_message", data, room=room_id, include_self=True)

            # Push notification logic will now be reached correctly.
            db = get_db()
            c = db.cursor()
            c.execute("""
                        SELECT ps.username, ps.fcm_token
                        FROM push_subscriptions ps
                        JOIN room_members rm ON ps.username = rm.username
                        LEFT JOIN user_room_notification_prefs prefs 
                            ON ps.username = prefs.username AND rm.room_unique_id = prefs.room_unique_id
                        LEFT JOIN users u ON ps.username = u.username
                        WHERE rm.room_unique_id = ? 
                        AND ps.username != ?
                        AND (prefs.notifications_enabled IS NULL OR prefs.notifications_enabled = 1)
                        AND (u.notifications_enabled IS NULL OR u.notifications_enabled = 1)
                    """, (room_id, username))
            all_member_tokens = c.fetchall()

            if not all_member_tokens: return

            active_users_in_this_room = set()
            for user, sids in user_to_sids.items():
                for sid in sids:
                    if sid_to_room.get(sid) == room_id:
                        active_users_in_this_room.add(user)
                        break 

            tokens_to_send = [
                row['fcm_token'] for row in all_member_tokens 
                if row['username'] not in active_users_in_this_room
            ]

            if not tokens_to_send:
                logging.info(f"Push notification for room {room_id} suppressed as all recipients are active.")
                return

            try:
                room_details = get_room_details(room_id)
                room_name = room_details['name'] if room_details else "Unknown Room"
                notification_url = url_for('chat', unique_id=room_id, _external=True)
                if notification_url.startswith('http://'):
                    notification_url = notification_url.replace('http://', 'https://', 1)

                push_message = messaging.MulticastMessage(
                    tokens=tokens_to_send,
                    webpush=messaging.WebpushConfig(
                        notification=messaging.WebpushNotification(
                            title=f"New message in {room_name}",
                            body=f"{username}: {message}",
                            tag=f"chat-{room_id}",
                            icon="/favicon.ico"
                        ),
                        fcm_options=messaging.WebpushFCMOptions(link=notification_url)
                    )
                )

                response = messaging.send_each_for_multicast(push_message)
                logging.info(f"FCM push sent to {len(tokens_to_send)} tokens: {response.success_count} success, {response.failure_count} failures.")

                if response.failure_count > 0:
                    failed_tokens = []
                    sent_token_map = {token: True for token in tokens_to_send}
                    original_tokens_that_were_sent = [t for t in all_member_tokens if t['fcm_token'] in sent_token_map]

                    for idx, resp in enumerate(response.responses):
                        if not resp.success and resp.exception.code in ('messaging/registration-token-not-registered', 'messaging/invalid-registration-token'):
                            failed_tokens.append(original_tokens_that_were_sent[idx]['fcm_token'])
                    
                    if failed_tokens:
                        logging.warning(f"Deleting {len(failed_tokens)} stale FCM tokens.")
                        c.execute(f"DELETE FROM push_subscriptions WHERE fcm_token IN ({','.join(['?']*len(failed_tokens))})", failed_tokens)
                        db.commit()

            except Exception as e:
                logging.error(f"Error sending FCM notification: {e}")
        else:
            logging.error(f"SAVE FAILED for {username} in {room_id}. Message was not broadcast.")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)
