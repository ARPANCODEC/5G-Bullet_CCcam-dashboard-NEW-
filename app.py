import os
import cv2
import csv
import time
import threading
from datetime import datetime
from flask import (
    Flask, render_template, Response,
    request, redirect, url_for, send_from_directory, send_file, flash, jsonify, session
)
from werkzeug.security import generate_password_hash, check_password_hash
import base64

app = Flask(__name__)
app.secret_key = "change-this-to-a-very-secret-key"

# ------------------- USER AUTHENTICATION -------------------
USERS_FILE = "users.csv"

if not os.path.exists(USERS_FILE):
    with open(USERS_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["username", "password_hash", "role", "registered_at"])
        admin_hash = generate_password_hash("admin123")
        writer.writerow(["admin", admin_hash, "admin", datetime.now().strftime("%Y-%m-%d %H:%M:%S")])

def load_users():
    users = {}
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                users[row['username']] = {
                    'password_hash': row['password_hash'],
                    'role': row['role'],
                    'registered_at': row['registered_at']
                }
    return users

def save_user(username, password_hash, role):
    users = load_users()
    if username in users:
        return False
    
    with open(USERS_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([username, password_hash, role, datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
    
    return True

def delete_user(username):
    users = load_users()
    if username not in users:
        return False
    
    rows = []
    with open(USERS_FILE, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = [row for row in reader if row[0] != username]
    
    with open(USERS_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)
    
    return True

def is_logged_in():
    return 'username' in session

def is_admin():
    return is_logged_in() and session.get('role') == 'admin'

def login_required(f):
    def decorated_function(*args, **kwargs):
        if not is_logged_in():
            flash("Please log in to access this page.")
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    decorated_function.__name__ = f.__name__
    return decorated_function

def admin_required(f):
    def decorated_function(*args, **kwargs):
        if not is_logged_in():
            flash("Please log in to access this page.")
            return redirect(url_for('login', next=request.url))
        if not is_admin():
            flash("Admin access required.")
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    decorated_function.__name__ = f.__name__
    return decorated_function

# ------------------- CONFIG -------------------
POSSIBLE_STREAMS = [
    "rtsp://admin:admin123@192.168.128.10:554/avstream/channel=1/stream=1.sdp",
    "http://192.168.128.10/video/mjpg",
    "http://192.168.128.10/axis-cgi/mjpg/video.cgi"
]

CAPTURE_DIR = "captures/images"
VIDEO_DIR = "captures/videos"
CSV_PATH = "captures/records.csv"

os.makedirs(CAPTURE_DIR, exist_ok=True)
os.makedirs(VIDEO_DIR, exist_ok=True)
os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)

if not os.path.exists(CSV_PATH):
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["filename", "reg_no", "name", "department", "timestamp"])

# ------------------- GLOBAL STATE -------------------
camera_stream = None
frame_lock = threading.Lock()
current_frame = None

frame_thread = None
frame_interval = 1/30

record_lock = threading.Lock()
recording = False
video_writer = None
current_video_filename = None

last_captured_filename = None

# ------------------- UTILITIES -------------------
def detect_camera_stream():
    for url in POSSIBLE_STREAMS:
        try:
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            if cap.isOpened():
                ret, frame = cap.read()
                cap.release()
                if ret and frame is not None:
                    app.logger.info(f"Detected stream: {url}")
                    return url
        except Exception as e:
            app.logger.warning(f"Error testing stream {url}: {e}")
            pass
    app.logger.warning("No camera stream detected from list.")
    return None

def test_custom_stream(stream_url):
    try:
        cap = cv2.VideoCapture(stream_url, cv2.CAP_FFMPEG)
        if cap.isOpened():
            ret, frame = cap.read()
            cap.release()
            return ret and frame is not None
    except Exception:
        pass
    return False

def start_frame_thread(stream_url):
    global frame_thread, camera_stream
    if frame_thread and frame_thread.is_alive():
        pass
    
    camera_stream = stream_url
    frame_thread = threading.Thread(target=frame_loop, args=(stream_url,), daemon=True)
    frame_thread.start()
    app.logger.info(f"Started frame thread for: {stream_url}")

def frame_loop(stream_url):
    global current_frame, recording, video_writer
    cap = cv2.VideoCapture(stream_url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        app.logger.error(f"Unable to open camera stream: {stream_url}")
        return
    
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    
    app.logger.info(f"Frame loop started for: {stream_url}")
    last_time = time.time()
    
    while camera_stream == stream_url:
        current_time = time.time()
        elapsed = current_time - last_time
        
        if elapsed > frame_interval:
            ret, frame = cap.read()
            if not ret or frame is None:
                time.sleep(0.01)
                continue
            
            with frame_lock:
                current_frame = frame.copy()
                with record_lock:
                    if recording and video_writer is not None:
                        try:
                            video_writer.write(frame)
                        except Exception as e:
                            app.logger.error(f"VideoWriter error: {e}")
            
            last_time = current_time
        else:
            time.sleep(max(0, frame_interval - (time.time() - current_time)))
    
    cap.release()
    app.logger.info(f"Frame loop ended for: {stream_url}")

def get_jpeg_bytes(quality=85):
    with frame_lock:
        if current_frame is None:
            return None
        frame = current_frame.copy()
    
    encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), quality]
    ret, buf = cv2.imencode(".jpg", frame, encode_param)
    if not ret:
        return None
    return buf.tobytes()

def append_csv_row(filename, reg_no="", name="", department=""):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([filename, reg_no, name, department, ts])

def remove_from_csv(filename):
    rows = []
    with open(CSV_PATH, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = [row for row in reader if row[0] != filename]
    
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)

# ------------------- AUTH ROUTES -------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if is_logged_in():
        return redirect(url_for('index'))
    
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        next_url = request.form.get("next", "").strip()
        
        users = load_users()
        if username in users and check_password_hash(users[username]['password_hash'], password):
            session['username'] = username
            session['role'] = users[username]['role']
            flash(f"Welcome back, {username}!")
            return redirect(next_url or url_for('index'))
        else:
            flash("Invalid username or password.")
    
    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if is_logged_in():
        return redirect(url_for('index'))
    
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        confirm_password = request.form.get("confirm_password", "").strip()
        
        if not username or not password:
            flash("Username and password are required.")
            return render_template("register.html")
        
        if password != confirm_password:
            flash("Passwords do not match.")
            return render_template("register.html")
        
        users = load_users()
        if username in users:
            flash("Username already exists.")
            return render_template("register.html")
        
        password_hash = generate_password_hash(password)
        if save_user(username, password_hash, "user"):
            flash("Registration successful. Please log in.")
            return redirect(url_for('login'))
        else:
            flash("Registration failed. Please try again.")
    
    return render_template("register.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.")
    return redirect(url_for('login'))

@app.route("/admin")
@admin_required
def admin_panel():
    users = []
    with open(USERS_FILE, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            users.append({
                'username': row['username'],
                'role': row['role'],
                'registered_at': row['registered_at']
            })
    
    admin_count = sum(1 for user in users if user['role'] == 'admin')
    user_count = sum(1 for user in users if user['role'] == 'user')
    
    return render_template(
        "admin.html", 
        users=users, 
        admin_count=admin_count, 
        user_count=user_count,
        camera_stream=camera_stream,
        recording=recording
    )

@app.route("/admin/delete_user", methods=["POST"])
@admin_required
def delete_user_route():
    username = request.form.get("username", "").strip()
    if not username:
        flash("Username is required.")
        return redirect(url_for('admin_panel'))
    
    if username == session.get('username'):
        flash("You cannot delete your own account.")
        return redirect(url_for('admin_panel'))
    
    users = load_users()
    if username not in users:
        flash("User not found.")
        return redirect(url_for('admin_panel'))
    
    if users[username]['role'] == 'admin':
        flash("Cannot delete admin users.")
        return redirect(url_for('admin_panel'))
    
    if delete_user(username):
        flash(f"User {username} deleted successfully.")
    else:
        flash(f"Failed to delete user {username}.")
    
    return redirect(url_for('admin_panel'))

# ------------------- APP ROUTES -------------------
@app.route("/")
@login_required
def index():
    return render_template(
        "index.html",
        stream_url=camera_stream,
        recording=recording,
        current_video=current_video_filename,
        last_image=last_captured_filename
    )

@app.route("/set_stream", methods=["POST"])
@login_required
def set_stream():
    stream_url = request.form.get("stream_url", "").strip()
    if not stream_url:
        flash("Please provide a stream URL")
        return redirect(url_for("index"))
    
    if test_custom_stream(stream_url):
        start_frame_thread(stream_url)
        flash(f"Stream set successfully: {stream_url}")
    else:
        flash(f"Unable to connect to stream: {stream_url}")
    
    return redirect(url_for("index"))

@app.route("/detect_stream", methods=["POST"])
@login_required
def detect_stream():
    detected_stream = detect_camera_stream()
    if detected_stream:
        start_frame_thread(detected_stream)
        flash(f"Auto-detected stream: {detected_stream}")
    else:
        flash("No stream could be auto-detected. Please enter a stream URL manually.")
    
    return redirect(url_for("index"))

@app.route("/video_feed")
@login_required
def video_feed():
    def gen():
        while True:
            jpg = get_jpeg_bytes(quality=80)
            if jpg is None:
                time.sleep(0.05)
                continue
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + jpg + b'\r\n')
            time.sleep(frame_interval)
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")

@app.route("/capture", methods=["POST"])
@login_required
def capture():
    global last_captured_filename
    if not camera_stream:
        flash("No camera stream configured.")
        return redirect(url_for("index"))
        
    reg_no = request.form.get("reg_no", "").strip()
    name = request.form.get("name", "").strip()
    dept = request.form.get("dept", "").strip()

    with frame_lock:
        if current_frame is None:
            flash("No frame available to capture.")
            return redirect(url_for("index"))
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_reg = reg_no.replace(" ", "_") or "reg"
        safe_name = name.replace(" ", "_") or "name"
        safe_dept = dept.replace(" ", "_") or "dept"
        
        # Create person directory if it doesn't exist
        person_dir = os.path.join(CAPTURE_DIR, f"{safe_reg}_{safe_name}")
        os.makedirs(person_dir, exist_ok=True)
        
        fname = f"{safe_reg}_{safe_name}_{safe_dept}_{ts}.jpg"
        path = os.path.join(person_dir, fname)
        cv2.imwrite(path, current_frame)
        last_captured_filename = fname
        append_csv_row(fname, reg_no, name, dept)

    flash(f"Captured: {fname}")
    return redirect(url_for("index"))

@app.route("/start_record", methods=["POST"])
@login_required
def start_record():
    global recording, video_writer, current_video_filename
    if not camera_stream:
        flash("No camera stream configured.")
        return redirect(url_for("index"))
        
    with frame_lock:
        if current_frame is None:
            flash("No frame available to start recording.")
            return redirect(url_for("index"))
        if recording:
            flash("Recording already in progress.")
            return redirect(url_for("index"))
        h, w = current_frame.shape[:2]
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        vname = f"record_{ts}.mp4"
        vpath = os.path.join(VIDEO_DIR, vname)
        
        fourcc = cv2.VideoWriter_fourcc(*'H264')
        video_writer = cv2.VideoWriter(vpath, fourcc, 15.0, (w, h))
        
        if not video_writer.isOpened():
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            video_writer = cv2.VideoWriter(vpath, fourcc, 15.0, (w, h))
            
            if not video_writer.isOpened():
                video_writer = None
                flash("Failed to start recording.")
                return redirect(url_for("index"))
        
        with record_lock:
            recording = True
            current_video_filename = vname
    flash(f"Recording started: {vname}")
    return redirect(url_for("index"))

@app.route("/stop_record", methods=["POST"])
@login_required
def stop_record():
    global recording, video_writer, current_video_filename
    with record_lock:
        if not recording:
            flash("Recording is not active.")
            return redirect(url_for("index"))
        recording = False
        if video_writer:
            vname = current_video_filename
            video_writer.release()
            video_writer = None
            current_video_filename = None
            flash(f"Recording saved: {vname}")
        else:
            flash("No video writer to stop.")
    return redirect(url_for("index"))

@app.route("/gallery")
@login_required
def gallery():
    # Get all images from person directories
    images = []
    for root, dirs, files in os.walk(CAPTURE_DIR):
        for file in files:
            if file.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.bmp')):
                rel_path = os.path.relpath(os.path.join(root, file), CAPTURE_DIR)
                images.append(rel_path)
    
    # Sort by modification time, newest first
    images.sort(key=lambda x: os.path.getmtime(os.path.join(CAPTURE_DIR, x)), reverse=True)
    
    videos = sorted(os.listdir(VIDEO_DIR), reverse=True)
    return render_template("gallery.html", images=images, videos=videos)

@app.route("/image/<path:filename>")
@login_required
def get_image(filename):
    return send_from_directory(CAPTURE_DIR, filename)

@app.route("/video/<path:filename>")
@login_required
def get_video(filename):
    return send_from_directory(VIDEO_DIR, filename)

@app.route("/save_metadata", methods=["POST"])
@login_required
def save_metadata():
    fname = request.form.get("filename")
    reg = request.form.get("reg_no", "").strip()
    name = request.form.get("name", "").strip()
    dept = request.form.get("department", "").strip()
    if not fname:
        flash("Missing filename.")
        return redirect(url_for("gallery"))
    append_csv_row(fname, reg, name, dept)
    flash(f"Saved metadata for {fname}")
    return redirect(url_for("gallery"))

@app.route("/delete/<file_type>/<path:filename>", methods=["POST"])
@login_required
def delete_file(file_type, filename):
    try:
        if file_type == "image":
            file_path = os.path.join(CAPTURE_DIR, filename)
            # Extract just the filename for CSV removal
            filename_only = os.path.basename(filename)
            remove_from_csv(filename_only)
        elif file_type == "video":
            file_path = os.path.join(VIDEO_DIR, filename)
        else:
            return jsonify({"success": False, "message": "Invalid file type"})
        
        if os.path.exists(file_path):
            os.remove(file_path)
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "message": "File not found"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@app.route("/download_csv")
@login_required
def download_csv():
    return send_file(CSV_PATH, as_attachment=True)

# ------------------- STARTUP -------------------
if __name__ == "__main__":
    detected_stream = detect_camera_stream()
    if detected_stream:
        start_frame_thread(detected_stream)
        app.logger.info(f"Auto-detected stream on startup: {detected_stream}")
    else:
        app.logger.warning("No camera stream detected on startup; user will need to configure manually.")
    
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)