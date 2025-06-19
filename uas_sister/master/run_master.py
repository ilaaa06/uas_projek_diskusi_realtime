# run_master.py 
import subprocess 
import sys 
import config 
 
def main(): 
    print(f"Starting Master Server on {config.HOST}:{config.PORT}") 
    print(f"Configured slaves: {config.SLAVE_SERVERS}") 
    print("Press Ctrl+C to stop the server") 
     
    try: 
        subprocess.run([sys.executable, 'app.py']) 
    except KeyboardInterrupt: 
        print("\nMaster server stopped.") 
 
if __name__ == '__main__': 
    main() 