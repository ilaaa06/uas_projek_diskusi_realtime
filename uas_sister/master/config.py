# Master Server Configuration 
SERVER_TYPE = 'master' 
HOST = '0.0.0.0'  # Listen on all interfaces 
PORT = 5000 
SECRET_KEY = 'master-secret-key-2024' 
# Slave servers (ganti dengan IP address slave yang sebenarnya) 
SLAVE_SERVERS = [ 
'http://172.10.10.244:5001',  # Ganti dengan IP Device 2 
# 'http://192.168.1.102:5001',  # Slave tambahan jika ada 
] 
# Database 
DB_PATH = 'master_discussion.db' 
# Sync settings 
SYNC_INTERVAL = 3 
SYNC_TIMEOUT = 10 
HEALTH_CHECK_INTERVAL = 60