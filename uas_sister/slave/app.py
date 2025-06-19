from flask import Flask, render_template, request, jsonify, redirect, url_for
import sqlite3
import requests
import threading
import time
import json
from datetime import datetime
import logging
import config

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = config.SECRET_KEY

# Database initialization dengan tabel tambahan
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
            sync_status TEXT DEFAULT 'pending',
            master_id INTEGER,  -- ID dari master server
            origin TEXT DEFAULT 'local'  -- 'local' atau 'master'
        )
    ''')
    
    # Tabel untuk tracking sync dengan master
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS master_sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER,
            sync_status TEXT DEFAULT 'pending',
            attempts INTEGER DEFAULT 0,
            last_attempt DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (message_id) REFERENCES messages (id)
        )
    ''')
    
    # Tabel untuk menyimpan timestamp terakhir sync
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sync_metadata (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
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
    cursor.execute('''
        INSERT INTO messages (username, message, origin) 
        VALUES (?, ?, 'local')
    ''', (username, message))
    message_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    # Sync ke master dengan retry mechanism
    threading.Thread(target=sync_to_master_with_retry, args=(message_id, username, message)).start()
    return message_id

def sync_to_master_with_retry(message_id, username, message, max_retries=5):
    """Sync ke master dengan retry dan exponential backoff"""
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()
    
    for attempt in range(max_retries):
        try:
            response = requests.post(
                f"{config.MASTER_SERVER}/sync_message",
                json={
                    'username': username,
                    'message': message,
                    'slave_id': message_id,
                    'timestamp': datetime.now().isoformat()
                },
                timeout=config.SYNC_TIMEOUT
            )
            
            if response.status_code == 200:
                # Update status sync sebagai success
                cursor.execute('''
                    UPDATE messages SET sync_status = 'synced' WHERE id = ?
                ''', (message_id,))
                
                cursor.execute('''
                    INSERT OR REPLACE INTO master_sync_log 
                    (message_id, sync_status, attempts, last_attempt)
                    VALUES (?, 'success', ?, ?)
                ''', (message_id, attempt + 1, datetime.now()))
                
                conn.commit()
                conn.close()
                
                logger.info(f"Successfully synced message {message_id} to master")
                return True
                
        except Exception as e:
            logger.error(f"Sync attempt {attempt + 1} failed: {str(e)}")
            
            # Record failed attempt
            cursor.execute('''
                INSERT OR REPLACE INTO master_sync_log 
                (message_id, sync_status, attempts, last_attempt)
                VALUES (?, 'failed', ?, ?)
            ''', (message_id, attempt + 1, datetime.now()))
            conn.commit()
            
            if attempt < max_retries - 1:
                # Exponential backoff
                sleep_time = (2 ** attempt) * 5  # 5, 10, 20, 40, 80 seconds
                logger.info(f"Retrying in {sleep_time} seconds...")
                time.sleep(sleep_time)
    
    # Semua attempt gagal
    cursor.execute('''
        UPDATE messages SET sync_status = 'failed' WHERE id = ?
    ''', (message_id,))
    conn.commit()
    conn.close()
    
    logger.error(f"Failed to sync message {message_id} after {max_retries} attempts")
    return False

def periodic_sync_check():
    """Periodic check untuk sync pesan yang gagal"""
    while True:
        try:
            conn = sqlite3.connect(config.DB_PATH)
            cursor = conn.cursor()
            
            # Check jika master online
            if is_master_online():
                # Ambil pesan yang belum tersinkronisasi
                cursor.execute('''
                    SELECT id, username, message 
                    FROM messages 
                    WHERE sync_status = 'failed' OR sync_status = 'pending'
                    AND origin = 'local'
                    ORDER BY timestamp ASC
                    LIMIT 10
                ''')
                
                failed_messages = cursor.fetchall()
                
                if failed_messages:
                    logger.info(f"Found {len(failed_messages)} unsynced messages, attempting to sync...")
                    
                    for msg_id, username, message in failed_messages:
                        # Reset status ke pending
                        cursor.execute('''
                            UPDATE messages SET sync_status = 'pending' WHERE id = ?
                        ''', (msg_id,))
                        conn.commit()
                        
                        # Attempt sync
                        threading.Thread(target=sync_to_master_with_retry, 
                                       args=(msg_id, username, message, 3)).start()
                        
                        time.sleep(1)  # Delay antar sync
                
                # Sync messages dari master yang mungkin terlewat
                sync_missing_messages_from_master()
            
            conn.close()
            
        except Exception as e:
            logger.error(f"Error in periodic sync check: {str(e)}")
        
        time.sleep(config.SYNC_INTERVAL)

def is_master_online():
    """Check apakah master server online"""
    try:
        response = requests.get(f"{config.MASTER_SERVER}/health", timeout=5)
        return response.status_code == 200
    except:
        return False

def sync_missing_messages_from_master():
    """Sync pesan dari master yang mungkin terlewat saat offline"""
    try:
        # Ambil timestamp terakhir sync
        conn = sqlite3.connect(config.DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT value FROM sync_metadata WHERE key = 'last_master_sync'
        ''')
        result = cursor.fetchone()
        
        if result:
            last_sync = result[0]
        else:
            # Jika belum pernah sync, ambil dari created_at database
            cursor.execute('''
                SELECT MIN(timestamp) FROM messages WHERE origin = 'master'
            ''')
            result = cursor.fetchone()
            last_sync = result[0] if result[0] else datetime.now().isoformat()
        
        # Request pesan baru dari master
        response = requests.get(
            f"{config.MASTER_SERVER}/api/messages",
            params={'since': last_sync},
            timeout=10
        )
        
        if response.status_code == 200:
            master_messages = response.json()
            new_messages = 0
            
            for msg in master_messages:
                # Check apakah pesan sudah ada
                cursor.execute('''
                    SELECT id FROM messages WHERE master_id = ? OR 
                    (username = ? AND message = ? AND timestamp = ?)
                ''', (msg['id'], msg['username'], msg['message'], msg['timestamp']))
                
                if not cursor.fetchone():
                    # Pesan baru, insert
                    cursor.execute('''
                        INSERT INTO messages (username, message, timestamp, master_id, origin, sync_status)
                        VALUES (?, ?, ?, ?, 'master', 'synced')
                    ''', (msg['username'], msg['message'], msg['timestamp'], msg['id']))
                    new_messages += 1
            
            if new_messages > 0:
                logger.info(f"Synced {new_messages} new messages from master")
                
                # Update timestamp terakhir sync
                cursor.execute('''
                    INSERT OR REPLACE INTO sync_metadata (key, value, updated_at)
                    VALUES ('last_master_sync', ?, ?)
                ''', (datetime.now().isoformat(), datetime.now()))
            
            conn.commit()
        
        conn.close()
        
    except Exception as e:
        logger.error(f"Error syncing from master: {str(e)}")

def master_health_monitor():
    """Monitor kesehatan master server"""
    master_status = {'online': False, 'last_check': None}
    
    while True:
        try:
            online = is_master_online()
            was_offline = not master_status['online']
            
            master_status['online'] = online
            master_status['last_check'] = datetime.now()
            
            if online:
                if was_offline:
                    logger.info("Master server is back online! Triggering sync...")
                    # Trigger immediate sync
                    threading.Thread(target=sync_missing_messages_from_master).start()
                else:
                    logger.debug("Master server is healthy")
            else:
                logger.warning("Master server is offline")
                
        except Exception as e:
            logger.error(f"Error monitoring master health: {str(e)}")
        
        time.sleep(30)  # Check setiap 30 detik

# API endpoints
@app.route('/')
def index():
    messages = get_messages()
    return render_template('index.html', messages=messages, server_type='Slave')

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
    """Receive message from master server"""
    try:
        data = request.get_json()
        username = data['username']
        message = data['message']
        master_id = data.get('master_id')
        timestamp = data.get('timestamp', datetime.now().isoformat())
        
        conn = sqlite3.connect(config.DB_PATH)
        cursor = conn.cursor()
        
        # Check apakah pesan sudah ada
        cursor.execute('''
            SELECT id FROM messages WHERE master_id = ? OR 
            (username = ? AND message = ? AND timestamp = ?)
        ''', (master_id, username, message, timestamp))
        
        if not cursor.fetchone():
            cursor.execute('''
                INSERT INTO messages (username, message, timestamp, master_id, origin, sync_status)
                VALUES (?, ?, ?, ?, 'master', 'synced')
            ''', (username, message, timestamp, master_id))
            conn.commit()
            logger.info(f"Received message from master: {master_id}")
        
        conn.close()
        return jsonify({'status': 'success'})
        
    except Exception as e:
        logger.error(f"Error receiving message from master: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/sync_status')
def get_sync_status():
    """Get sync status info"""
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()
    
    # Hitung pesan berdasarkan status
    cursor.execute('''
        SELECT sync_status, COUNT(*) as count
        FROM messages
        WHERE origin = 'local'
        GROUP BY sync_status
    ''')
    sync_stats = dict(cursor.fetchall())
    
    # Last sync info
    cursor.execute('''
        SELECT value FROM sync_metadata WHERE key = 'last_master_sync'
    ''')
    result = cursor.fetchone()
    last_sync = result[0] if result else None
    
    conn.close()
    
    return jsonify({
        'sync_statistics': sync_stats,
        'last_master_sync': last_sync,
        'master_online': is_master_online(),
        'timestamp': datetime.now().isoformat()
    })

@app.route('/health')
def health():
    return jsonify({
        'status': 'healthy',
        'server_type': 'slave',
        'master_server': config.MASTER_SERVER,
        'master_online': is_master_online(),
        'timestamp': datetime.now().isoformat(),
        'message_count': len(get_messages())
    })

if __name__ == '__main__':
    init_db()
    
    # Start background processes
    sync_thread = threading.Thread(target=periodic_sync_check, daemon=True)
    sync_thread.start()
    
    health_thread = threading.Thread(target=master_health_monitor, daemon=True)
    health_thread.start()
    
    logger.info(f"Starting Enhanced Slave Server on {config.HOST}:{config.PORT}")
    logger.info(f"Master Server: {config.MASTER_SERVER}")
    logger.info("Features: Offline sync, retry mechanism, health monitoring")
    
    app.run(host=config.HOST, port=config.PORT, debug=True)