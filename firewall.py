#!/usr/bin/env python3

"""
Combined Dynamic Firewall with:
- Network-level IPS/IDS (packet sniffing with Scapy and ML-based detection)
- Application-level firewall with DDoS prevention, AI- & rule-based detection,
  and a live dashboard (Flask).
"""

import os
import sys
import re
import subprocess
import time
import json
import joblib
import threading
from collections import defaultdict, deque
from flask import Flask, render_template, request, make_response, redirect, url_for , jsonify , session , flash, abort
import logging
import numpy as np
from scapy.all import sniff, IP, TCP, Raw
import psutil
import random
import secureauth

# Import the AI detector (must be present in PYTHONPATH)
from ai_detector import detect_attack

# ---- OS DETECTION -------------------
from os_detection import detect_os
current_os = detect_os()  # 'linux', 'windows', 'darwin'
# --------------------------------------

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    filename="firewall.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# ---------------------------------------------------------------------------
# Environment / globals
# ---------------------------------------------------------------------------
ddos_limiter = None                             # initialised further below
stats = None
ip_request_count = defaultdict(int)             # {ip: hits}
blocked_ips = set()                             # in-memory (also persisted to blocked.txt)

##############################################################################
# BLOCKED IPs LOADING & ENFORCEMENT
##############################################################################

def load_blocked_ips():
    blocked = set()
    try:
        with open("blocked.txt", "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    ip = line.split(",")[0]
                    blocked.add(ip)
        logging.info(f"Loaded {len(blocked)} blocked IPs from blocked.txt")
    except FileNotFoundError:
        pass
    return blocked

def enforce_os_block(ip):
    """Enforce OS-level block for the given IP regardless of current memory."""
    if current_os in ("linux","ubuntu"):
        cmd = f"sudo iptables -C INPUT -s {ip} -j DROP 2>/dev/null || sudo iptables -A INPUT -s {ip} -j DROP"
    elif current_os == "windows":
        cmd = f'netsh advfirewall firewall add rule name="Block {ip}" dir=in action=block remoteip={ip}'
    elif current_os == "darwin":
        cmd = f"echo 'block drop from {ip} to any' | sudo pfctl -ef -"
    else:
        logging.warning(f"Unsupported OS '{current_os}' – cannot block {ip} automatically.")
        return
    logging.info(f"Enforcing OS firewall block for blocked IP {ip}:\n  {cmd}")
    os.system(cmd)

# Initially load blocked IPs from file and enforce
blocked_ips = load_blocked_ips()
for ip in blocked_ips:
    enforce_os_block(ip)

##############################################################################
# Flask app definition and before_request to block denied IPs
##############################################################################
app = Flask(__name__)
app.config['DEBUG'] = True
app.secret_key = 'Ja0RSgXjEotzDuPEVP4aS3jyQg3EUaKN'

@app.before_request
def deny_blocked_ips():
    client_ip = request.remote_addr or "unknown"
    # Re-load live in case blocked.txt is edited externally (optional, but robust)
    # global blocked_ips  # Only needed if you want to re-read every request! (slower)
    # blocked_ips = load_blocked_ips()
    if client_ip in blocked_ips:
        enforce_os_block(client_ip)
        log_request_details(client_ip, "<BLOCKED>", "denied (in blocked.txt)")
        abort(403)

##############################################################################
#                              DDoS Limiter                                  #
##############################################################################
class DDoSRateLimiter:
    def __init__(self, time_window: int = 60, max_requests: int = 20):
        self.time_window = time_window
        self.max_requests = max_requests
        self.requests_log = defaultdict(deque)  # {ip: deque[timestamps]}

    def is_ddos(self, ip: str) -> bool:
        now = time.time()
        dq = self.requests_log[ip]
        # Remove timestamps older than window
        while dq and dq[0] < now - self.time_window:
            dq.popleft()
        if len(dq) >= self.max_requests:
            return True
        dq.append(now)
        return False

##############################################################################
#                                 Stats                                      #
##############################################################################
class FirewallStats:
    def __init__(self) -> None:
        self.total_requests = 0
        self.allowed_requests = 0
        self.blocked_requests = 0
        self.ddos_blocks = 0
        self.rule_based_blocks = 0
        self.ai_based_blocks = 0
        self.network_blocks = 0   # network-level IPS/IDS

    def to_dict(self) -> dict:
        return {
            "total_requests": self.total_requests,
            "allowed_requests": self.allowed_requests,
            "blocked_requests": self.blocked_requests,
            "ddos_blocks": self.ddos_blocks,
            "rule_based_blocks": self.rule_based_blocks,
            "ai_based_blocks": self.ai_based_blocks,
            "network_blocks": self.network_blocks,
        }

ddos_limiter = DDoSRateLimiter(time_window=60, max_requests=20)
stats = FirewallStats()

##############################################################################
#                        Rule-based App-level detection                      #
##############################################################################
attack_patterns = {
    "sql_injection":  r"(\bSELECT\b|\bUNION\b|\bDROP\b|\bINSERT\b|\bUPDATE\b|\bDELETE\b|\bOR\s+1=1\b|\bWHERE\s+1=1\b|--)",
    "xss":            r"(<script>|alert\(|onerror=)",
    "path_traversal": r"(\.\./|\b/etc/passwd\b)",
}

def rule_based_detect(data: str):
    for attack, pattern in attack_patterns.items():
        if re.search(pattern, data, re.IGNORECASE):
            return True, attack
    return False, None

##############################################################################
#                       Network-level IPS/IDS helpers                        #
##############################################################################
def extract_features(packet):
    feats = []
    if packet.haslayer(IP):
        ip_layer = packet[IP]
        feats.extend((ip_layer.len, ip_layer.ttl))
    if packet.haslayer(TCP):
        tcp_layer = packet[TCP]
        feats.extend((int(tcp_layer.flags), tcp_layer.sport, tcp_layer.dport))
    payload_len = len(packet[Raw].load) if packet.haslayer(Raw) else 0
    feats.append(payload_len)
    return np.array(feats).reshape(1, -1)

def ml_predict(packet) -> bool:
    if packet.haslayer(Raw):
        try:
            payload = packet[Raw].load.decode("utf-8", errors="ignore")
            if any(kw in payload.upper() for kw in ("DROP TABLE", "SELECT * FROM", "OR 1=1")):
                return True
        except Exception as exc:
            logging.error(f"Payload decode error: {exc}")
    return False

def block_ip(ip_address: str) -> None:
    if ip_address in blocked_ips:
        return
    if current_os == "linux":
        cmd = f"sudo iptables -A INPUT -s {ip_address} -j DROP"
    elif current_os == "windows":
        cmd = f'netsh advfirewall firewall add rule name="Block {ip_address}" dir=in action=block remoteip={ip_address}'
    elif current_os == "darwin":
        cmd = f"echo 'block drop from {ip_address} to any' | sudo pfctl -ef -"
    else:
        logging.warning(f"Unsupported OS '{current_os}' – cannot block {ip_address} automatically.")
        return
    logging.info(f"Blocking IP {ip_address} using command:\n  {cmd}")
    os.system(cmd)
    stats.network_blocks += 1
    add_blocked_ip(ip_address)

def process_packet(packet):
    if not packet.haslayer(IP):
        return
    src_ip = packet[IP].src
    dst_ip = packet[IP].dst
    if ml_predict(packet):
        logging.warning(f"Network-level malicious packet detected: {src_ip} → {dst_ip} – blocking.")
        block_ip(src_ip)

def start_packet_sniffing():
    logging.info("Starting Scapy sniffing thread (network-level IPS/IDS)…")
    sniff(filter="ip", prn=process_packet, store=0)

def get_cpu_usage():
    total_cpu = psutil.cpu_percent(interval=1)
    core_count = psutil.cpu_count(logical=True)
    return {
        "total": total_cpu,
        "cores": core_count
    }

def get_uptime():
    boot_time = psutil.boot_time()
    current_time = time.time()
    uptime_seconds = current_time - boot_time
    uptime_days = int(uptime_seconds // (24 * 3600))
    uptime_hours = int((uptime_seconds % (24 * 3600)) // 3600)
    uptime_minutes = int((uptime_seconds % 3600) // 60)
    uptime_seconds = int(uptime_seconds % 60)
    return {
        "uptime_seconds": int(current_time - boot_time),
        "formatted": f"{uptime_days}d {uptime_hours}h {uptime_minutes}m {uptime_seconds}s"
    }

def get_ram_usage():
    memory = psutil.virtual_memory()
    return {
        "total": round(memory.total / (1024**3), 2),  # GB
        "used": round(memory.used / (1024**3), 2),
        "free": round(memory.free / (1024**3), 2),
        "percent": memory.percent
    }

def get_disk_usage():
    try:
        usage = psutil.disk_usage('/')
        disk_data = [{
            "device": "root",
            "mountpoint": "/",
            "total": round(usage.total / (1024**3), 2),  # GB
            "used": round(usage.used / (1024**3), 2),
            "free": round(usage.free / (1024**3), 2),
            "percent": usage.percent
        }]
        return disk_data
    except PermissionError:
        return []  # Return empty list if permission denied

@app.route('/metrics')
def metrics():
    data = {
        "cpu": get_cpu_usage(),
        "ram": get_ram_usage(),
        "disk": get_disk_usage(),
        "uptime":get_uptime(),
        "timestamp": time.time()
    }
    return jsonify(data)

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        try:
            if secureauth.verify_user(username, password):
                session['user'] = username
                flash("Login successful!", "success")
                return redirect(url_for('dashboard'))
            else:
                error = "Invalid username or password"
                flash(error, "error")
        except Exception as exc:
            error = f"Login error: {exc}"
            flash(error, "error")
    return render_template('login.html')

@app.route("/stats")
def stats_endpoint():
    return jsonify(stats.to_dict())

@app.route("/top_ips")
def top_ips():
    top5 = sorted(ip_request_count.items(), key=lambda kv: kv[1], reverse=True)[:5]
    return jsonify(dict(top5))

@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        flash("You must be logged in to access the dashboard.", "error")
        return redirect(url_for("login"))
    top_ips, top_reasons = get_top_blocked_ips_and_reasons()
    try:
        return render_template(
            "dashboard.html",
            top_ips=top_ips,
            top_reasons=top_reasons
        )
    except Exception:
        return jsonify({
            "message": "Dashboard template not found – create templates/dashboard.html to enable the UI.",
            "stats": stats.to_dict(),
            "top_ips": dict(sorted(ip_request_count.items(), key=lambda kv: kv[1], reverse=True)[:5])
        })

@app.route("/", defaults={"path": ""}, methods=["GET", "POST"])
@app.route("/<path:path>", methods=["GET", "POST"])
def firewall_route(path):
    client_ip = request.remote_addr or "unknown"
    stats.total_requests += 1
    ip_request_count[client_ip] += 1
    # --- 1) DDoS limiter ----------------------------------------------------
    if ddos_limiter.is_ddos(client_ip):
        stats.blocked_requests += 1
        stats.ddos_blocks += 1
        reason = "DDoS detected (rate limit)"  # ← FIXED: define `reason` first
        add_blocked_ip(client_ip, reason)
        log_request_details(client_ip, "<rate-limited>", f"blocked – {reason}")
        return jsonify({"status": "blocked", "reason": reason}), 429
    # --- 2) Payload inspection ---------------------------------------------
    data = request.get_data(as_text=True) or ""
    referer = request.headers.get('Referer', '')
    is_refresh = referer and ('/dashboard' in referer or path in referer)
    if is_refresh:
        stats.allowed_requests += 1
        log_request_details(client_ip, data, "allowed (refresh)")
        return jsonify({"status": "allowed", "echo": data})
    # Rule-based detection
    rule_flag, attack_type = rule_based_detect(data)
    ai_label = detect_attack(data)
    # AI detector output interpretation
    if ai_label != 0:
        stats.blocked_requests += 1
        stats.ai_based_blocks += 1
        reason = {1: "SQLi (AI)", 2: "XSS (AI)", 3: "DDoS (AI)"}.get(ai_label, "Anomaly (AI)")
        add_blocked_ip(client_ip,reason)
        log_request_details(client_ip, data, f"blocked – {reason}")
        return jsonify({"status": "blocked", "reason": reason}), 403
    if rule_flag:
        stats.blocked_requests += 1
        stats.rule_based_blocks += 1
        reason = attack_type  # ← FIXED: define `reason` first
        add_blocked_ip(client_ip, reason)
        log_request_details(client_ip, data, f"blocked – {reason}")
        return jsonify({"status": "blocked", "reason": reason}), 403
    stats.allowed_requests += 1
    log_request_details(client_ip, data, "allowed")
    return jsonify({"status": "allowed", "echo": data})

##############################################################################
#                           Helper functions                                 #
##############################################################################
def log_request_details(ip: str, data: str, result: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {ip} - {result} - {data}\n"
    try:
        with open("firewall.log", "a") as f:
            f.write(line)
    except Exception as exc:
        print(f"Log write error: {exc}")

def add_blocked_ip(ip: str, reason: str) -> None:
    if ip in blocked_ips:
        return
    blocked_ips.add(ip)
    enforce_os_block(ip)
    try:
        with open("blocked.txt", "a") as f:
            f.write(f"{ip},{reason}\n")
    except Exception as exc:
        print(f"blocked.txt write error: {exc}")

def get_top_blocked_ips_and_reasons(n: int = 5):
    try:
        with open("blocked.txt", "r") as f:
            lines = [line.strip() for line in f if line.strip()]
        recent_entries = lines[-n:]
        ips = [entry.split(",")[0] for entry in recent_entries]
        reasons = [entry.split(",", 1)[1] if "," in entry else "Unknown" for entry in recent_entries]
        return ips, reasons
    except FileNotFoundError:
        return [], []

def print_statistics() -> None:
    sep = "-" * 48
    print(f"\n{sep}\n[DYNAMIC UPDATE @ {time.strftime('%H:%M:%S')}]\n{sep}")
    for k, v in stats.to_dict().items():
        print(f"{k.replace('_', ' ').title():22}: {v}")
    print("Top 5 IPs:")
    for ip, cnt in sorted(ip_request_count.items(), key=lambda kv: kv[1], reverse=True)[:5]:
        print(f"  {ip:>15}  -> {cnt}")
    print(sep)

def dynamic_update() -> None:
    while True:
        time.sleep(10)
        print_statistics()

def run_server() -> None:
    logging.info("Starting Flask server on 0.0.0.0:8080")
    app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=False)

def deployment_helper() -> None:
    print("\n=== Deployment Summary ===")
    print(f"Operating System  : {current_os}")
    print("Network IPS/IDS   : ACTIVE")
    print("Flask Web Server  : http://0.0.0.0:8080")
    print("Dashboard         : http://0.0.0.0:8080/dashboard")
    print("Top IPs Endpoint  : http://0.0.0.0:8080/top_ips")
    print("Log File          : firewall.log")
    print("Blocked IPs File  : blocked.txt")
    print("===========================================\n")

##############################################################################
#                                Main                                        #
##############################################################################
if __name__ == "__main__":
    sniff_thread = threading.Thread(target=start_packet_sniffing, daemon=True)
    sniff_thread.start()
    dyn_thread = threading.Thread(
        target=lambda: (time.sleep(2), deployment_helper(), dynamic_update()),
        daemon=True
    )
    dyn_thread.start()
    flask_thread = threading.Thread(target=run_server, daemon=True)
    flask_thread.start()
    print("Firewall server running on http://0.0.0.0:8080")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Quitting and running in the background…")
        subprocess.Popen(
            [sys.executable] + sys.argv,
            preexec_fn=os.setsid,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            close_fds=True
        )
        os._exit(0)
