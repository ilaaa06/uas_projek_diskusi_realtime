# Slave Server Configuration 
SERVER_TYPE = 'slave' 
HOST = '0.0.0.0'  # Listen on all interfaces 
PORT = 5001 
SECRET_KEY = 'slave-secret-key-2024' 
# Master server (ganti dengan IP address master yang sebenarnya) 
MASTER_SERVER = 'http://172.10.10.214:5000'  # Ganti dengan IP Device 1 
# Database 
DB_PATH = 'slave_discussion.db' 
# Sync settings 
SYNC_INTERVAL = 3  # Sync every 3 seconds 
SYNC_TIMEOUT = 10 
HEALTH_CHECK_INTERVAL = 60