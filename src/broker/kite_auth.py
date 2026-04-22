import os
import json
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
import webbrowser
import threading
from kiteconnect import KiteConnect
from dotenv import load_dotenv

# Run this script once a day to authenticate and capture the token.

class KiteLoginHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        query_components = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        
        if "request_token" in query_components:
            request_token = query_components["request_token"][0]
            print(f"\n[+] Received Request Token: {request_token}")
            
            try:
                # Exchange request token for access token
                data = self.server.kite.generate_session(request_token, api_secret=self.server.api_secret)
                
                # Save the entire session object to a cache file
                def datetime_handler(x):
                    if hasattr(x, 'isoformat'):
                        return x.isoformat()
                    return str(x)

                with open("kite_session.json", "w") as f:
                    json.dump(data, f, default=datetime_handler)
                    
                print(f"[+] Successfully generated Access Token and saved to kite_session.json!")
                
                self.send_response(200)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><head><style>body{font-family:sans-serif; text-align:center; padding-top:50px; background-color:#121212; color:#00FF00;}</style></head><body><h1>Login Successful!</h1><p>The Trading Agent has captured the token. You can close this tab now.</p></body></html>")
            except Exception as e:
                print(f"[-] Error generating session: {e}")
                self.send_response(500)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(f"<html><body><h1>Error</h1><p>{e}</p></body></html>".encode('utf-8'))
                
            # Stop the server gracefully without killing the parent process
            print("[*] Shutting down authentication server...")
            threading.Thread(target=self.server.shutdown).start()
            
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"No request_token found.")

def start_login_process(port=8000):
    load_dotenv()
    KITE_API_KEY = os.getenv("KITE_API_KEY")
    KITE_API_SECRET = os.getenv("KITE_API_SECRET")

    if not KITE_API_KEY or not KITE_API_SECRET or KITE_API_KEY == "your_kite_api_key":
        print("Error: KITE_API_KEY or KITE_API_SECRET is missing or invalid in .env")
        return

    kite = KiteConnect(api_key=KITE_API_KEY)
    
    print("\n--- Zerodha Kite Daily Authentication ---")
    print(f"1. Ensure your App's Redirect URL in Zerodha Developer Console is exactly: http://127.0.0.1:{port}/")
    
    login_url = kite.login_url()
    print(f"\n2. Opening browser to login to Kite: {login_url}")
    webbrowser.open(login_url)
    
    print("\n3. Waiting for redirect...")
    server = HTTPServer(('127.0.0.1', port), KiteLoginHandler)
    server.kite = kite # Inject kite instance
    server.api_secret = KITE_API_SECRET # Inject api_secret
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nAuthentication server stopped manually.")
    finally:
        server.server_close()

if __name__ == "__main__":
    start_login_process()
