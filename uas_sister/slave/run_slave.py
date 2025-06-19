# run_slave.py
import subprocess
import sys
import config

def main():
    print(f"Starting Slave Server on {config.HOST}:{config.PORT}")
    print(f"Master server: {config.MASTER_SERVER}")
    print("Press Ctrl+C to stop the server")
    
    try:
        subprocess.run([sys.executable, 'app.py'])
    except KeyboardInterrupt:
        print("\nSlave server stopped.")

if __name__ == '__main__':
    main()