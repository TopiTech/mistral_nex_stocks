import urllib.request
import time
import json

url_stream = "http://127.0.0.1:5000/api/stocks/stream"
url_debug = "http://127.0.0.1:5000/api/debug/threads"

connections = []
print("Opening 5 parallel connections...")
for i in range(5):
    try:
        req = urllib.request.urlopen(url_stream, timeout=2.0)
        # Read the first line (headers/initial snapshot)
        req.readline()
        connections.append(req)
        print(f"Connection {i+1} opened.")
    except Exception as e:
        print(f"Connection {i+1} failed: {e}")

print("\nActive threads before close:")
try:
    req = urllib.request.urlopen(url_debug, timeout=5.0)
    data = json.loads(req.read().decode())
    req.close()
    stream_threads = [t for t in data if "stream" in "".join(t["stack"]).lower()]
    print(f"Found {len(stream_threads)} stream threads.")
except Exception as e:
    print("Failed to get thread trace:", e)

print("\nClosing all 5 connections...")
for req in connections:
    req.close()

# Wait 5 seconds for cleanup
print("Waiting 5 seconds for server to detect close...")
time.sleep(5.0)

print("\nActive threads after close:")
try:
    req = urllib.request.urlopen(url_debug, timeout=5.0)
    data = json.loads(req.read().decode())
    req.close()
    stream_threads = [t for t in data if "stream" in "".join(t["stack"]).lower()]
    print(f"Found {len(stream_threads)} stream threads.")
except Exception as e:
    print("Failed to get thread trace:", e)
