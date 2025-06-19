from flask import Flask, render_template, request, jsonify, redirect, url_for
import sqlite3
import requests
import threading
import time
import json
from datetime import datetime
import logging
import config
from queue import Queue

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = config.SECRET_KEY

# Message queue untuk offline messages
message_queue = Queue()
failed_sync_queue = {}  # {slave_url: [messages]}

# Database initialization dengan tabel tambahan untuk tracking sync
def init_db():
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()
    
    # Tabel messages utama
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            message TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            sync_status TEXT DEFAULT 'pending'
        )
    ''')
    
    # Tabel untuk tracking sync status per slave
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sync_status (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER,
            slave_url TEXT,
            sync_status TEXT DEFAULT 'pending',
            attempts INTEGER DEFAULT 0,
            last_attempt DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (message_id) REFERENCES messages (id)
        )
    ''')
    
    # Tabel untuk menyimpan pesan offline
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS offline_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER,
            slave_url TEXT,
            message_data TEXT,  -- JSON string
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (message_id) REFERENCES messages (id)
        )
    ''')
    
    conn.commit()
    conn.close()

def get_messages():
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT id, username, message, timestamp FROM messages ORDER BY timestamp DESC')
    messages = cursor.fetchall()
    conn.close()
    return messages

def add_message(username, message):
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()
    cursor.execute('INSERT INTO messages (username, message) VALUES (?, ?)', (username, message))
    message_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    # Queue pesan untuk sinkronisasi
    message_data = {
        'id': message_id,
        'username': username,
        'message': message,
        'timestamp': datetime.now().isoformat()
    }
    
    # Sync ke slaves dengan queue system
    threading.Thread(target=sync_to_slaves_with_queue, args=(message_data,)).start()
    return message_id

def sync_to_slaves_with_queue(message_data):
    """Sync dengan retry mechanism dan offline queue"""
    for slave_url in config.SLAVE_SERVERS:
        success = attempt_sync_to_slave(slave_url, message_data)
        
        if not success:
            # Simpan ke offline queue
            save_offline_message(message_data['id'], slave_url, message_data)
            logger.warning(f"Message {message_data['id']} queued for offline sync to {slave_url}")

def attempt_sync_to_slave(slave_url, message_data, max_retries=3):
    """Attempt to sync message to slave with retries"""
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()
    
    for attempt in range(max_retries):
        try:
            response = requests.post(
                f"{slave_url}/sync_message",
                json={
                    'username': message_data['username'],
                    'message': message_data['message'],
                    'master_id': message_data['id'],
                    'timestamp': message_data['timestamp']
                },
                timeout=config.SYNC_TIMEOUT
            )
            
            if response.status_code == 200:
                # Update sync status sebagai success
                cursor.execute('''
                    INSERT OR REPLACE INTO sync_status 
                    (message_id, slave_url, sync_status, attempts, last_attempt)
                    VALUES (?, ?, 'success', ?, ?)
                ''', (message_data['id'], slave_url, attempt + 1, datetime.now()))
                conn.commit()
                conn.close()
                
                logger.info(f"Successfully synced message {message_data['id']} to {slave_url}")
                return True
                
        except Exception as e:
            logger.error(f"Attempt {attempt + 1} failed for {slave_url}: {str(e)}")
            time.sleep(2 ** attempt)  # Exponential backoff
    
    # Semua attempt gagal
    cursor.execute('''
        INSERT OR REPLACE INTO sync_status 
        (message_id, slave_url, sync_status, attempts, last_attempt)
        VALUES (?, ?, 'failed', ?, ?)
    ''', (message_data['id'], slave_url, max_retries, datetime.now()))
    conn.commit()
    conn.close()
    
    return False

def save_offline_message(message_id, slave_url, message_data):
    """Simpan pesan ke offline queue"""
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO offline_messages (message_id, slave_url, message_data)
        VALUES (?, ?, ?)
    ''', (message_id, slave_url, json.dumps(message_data)))
    conn.commit()
    conn.close()

def process_offline_queue():
    """Background process untuk mengirim pesan offline"""
    while True:
        try:
            conn = sqlite3.connect(config.DB_PATH)
            cursor = conn.cursor()
            
            # Ambil pesan offline yang belum terkirim
            cursor.execute('''
                SELECT id, message_id, slave_url, message_data 
                FROM offline_messages 
                ORDER BY created_at ASC
            ''')
            offline_messages = cursor.fetchall()
            
            for offline_id, message_id, slave_url, message_data_json in offline_messages:
                message_data = json.loads(message_data_json)
                
                # Coba kirim lagi
                if attempt_sync_to_slave(slave_url, message_data, max_retries=1):
                    # Berhasil, hapus dari offline queue
                    cursor.execute('DELETE FROM offline_messages WHERE id = ?', (offline_id,))
                    conn.commit()
                    logger.info(f"Offline message {message_id} successfully synced to {slave_url}")
            
            conn.close()
            
        except Exception as e:
            logger.error(f"Error processing offline queue: {str(e)}")
        
        time.sleep(30)  # Check setiap 30 detik

def check_slave_health_enhanced():
    """Enhanced health check yang juga trigger offline sync"""
    while True:
        conn = sqlite3.connect(config.DB_PATH)
        cursor = conn.cursor()
        
        for slave_url in config.SLAVE_SERVERS:
            try:
                response = requests.get(f"{slave_url}/health", timeout=5)
                if response.status_code == 200:
                    logger.info(f"Slave {slave_url} is healthy")
                    
                    # Slave online, coba kirim pesan offline
                    cursor.execute('''
                        SELECT COUNT(*) FROM offline_messages WHERE slave_url = ?
                    ''', (slave_url,))
                    offline_count = cursor.fetchone()[0]
                    
                    if offline_count > 0:
                        logger.info(f"Found {offline_count} offline messages for {slave_url}, processing...")
                        threading.Thread(target=sync_offline_messages_for_slave, args=(slave_url,)).start()
                        
                else:
                    logger.warning(f"Slave {slave_url} returned status {response.status_code}")
                    
            except Exception as e:
                logger.error(f"Slave {slave_url} is unreachable: {str(e)}")
        
        conn.close()
        time.sleep(config.HEALTH_CHECK_INTERVAL)

def sync_offline_messages_for_slave(slave_url):
    """Sync semua pesan offline untuk slave tertentu"""
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT id, message_id, message_data 
        FROM offline_messages 
        WHERE slave_url = ?
        ORDER BY created_at ASC
    ''', (slave_url,))
    
    offline_messages = cursor.fetchall()
    
    for offline_id, message_id, message_data_json in offline_messages:
        message_data = json.loads(message_data_json)
        
        if attempt_sync_to_slave(slave_url, message_data, max_retries=1):
            cursor.execute('DELETE FROM offline_messages WHERE id = ?', (offline_id,))
            conn.commit()
            logger.info(f"Synced offline message {message_id} to {slave_url}")
        else:
            break  # Jika gagal, stop untuk mencegah spam
    
    conn.close()

# API endpoint untuk mendapatkan sync status
@app.route('/api/sync_status')
def get_sync_status():
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()
    
    # Hitung pesan yang belum tersinkronisasi
    cursor.execute('''
        SELECT slave_url, COUNT(*) as pending_count
        FROM offline_messages
        GROUP BY slave_url
    ''')
    offline_stats = dict(cursor.fetchall())
    
    # Status slave terakhir
    cursor.execute('''
        SELECT slave_url, sync_status, COUNT(*) as count
        FROM sync_status
        WHERE last_attempt > datetime('now', '-1 hour')
        GROUP BY slave_url, sync_status
    ''')
    sync_stats = cursor.fetchall()
    
    conn.close()
    
    return jsonify({
        'offline_messages': offline_stats,
        'sync_statistics': sync_stats,
        'timestamp': datetime.now().isoformat()
    })

# Routes yang sudah ada (sama seperti sebelumnya)
@app.route('/')
def index():
    messages = get_messages()
    return render_template('index.html', messages=messages, server_type='Master')

@app.route('/send_message', methods=['POST'])
def send_message():
    username = request.form.get('username', '').strip()
    message = request.form.get('message', '').strip()
    
    if not username or not message:
        return redirect(url_for('index'))
    
    add_message(username, message)
    return redirect(url_for('index'))

@app.route('/api/messages')
def api_messages():
    messages = get_messages()
    return jsonify([{
        'id': msg[0],
        'username': msg[1],
        'message': msg[2],
        'timestamp': msg[3]
    } for msg in messages])

@app.route('/sync_message', methods=['POST'])
def sync_message():
    """Receive message from slave server"""
    try:
        data = request.get_json()
        username = data['username']
        message = data['message']
        slave_id = data.get('slave_id')
        
        conn = sqlite3.connect(config.DB_PATH)
        cursor = conn.cursor()
        cursor.execute('INSERT INTO messages (username, message, sync_status) VALUES (?, ?, ?)',
                      (username, message, 'synced'))
        master_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        # Sync ke slaves lain dengan queue system
        message_data = {
            'id': master_id,
            'username': username,
            'message': message,
            'timestamp': datetime.now().isoformat()
        }
        
        threading.Thread(target=sync_to_other_slaves, args=(message_data, request.remote_addr)).start()
        
        return jsonify({'status': 'success', 'master_id': master_id})
        
    except Exception as e:
        logger.error(f"Error syncing message: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

def sync_to_other_slaves(message_data, sender_ip):
    """Sync ke slaves lain kecuali pengirim"""
    for slave_url in config.SLAVE_SERVERS:
        # Skip sender (naive check berdasarkan IP)
        if sender_ip not in slave_url:
            success = attempt_sync_to_slave(slave_url, message_data)
            if not success:
                save_offline_message(message_data['id'], slave_url, message_data)

@app.route('/health')
def health():
    return jsonify({
        'status': 'healthy',
        'server_type': 'master',
        'timestamp': datetime.now().isoformat(),
        'message_count': len(get_messages())
    })

if __name__ == '__main__':
    init_db()
    
    # Start background processes
    health_thread = threading.Thread(target=check_slave_health_enhanced, daemon=True)
    health_thread.start()
    
    offline_queue_thread = threading.Thread(target=process_offline_queue, daemon=True)
    offline_queue_thread.start()
    
    logger.info(f"Starting Enhanced Master Server on {config.HOST}:{config.PORT}")
    logger.info("Features: Offline messaging, message queue, retry mechanism")
    
    app.run(host=config.HOST, port=config.PORT, debug=True)