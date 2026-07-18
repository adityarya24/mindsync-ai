import subprocess
import json
import sys

def main():
    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"}
        }
    }
    
    print("Testing mindsync stdio...")
    try:
        proc = subprocess.Popen(
            ["mindsync"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        print("mindsync entrypoint not found!")
        sys.exit(1)
        
    out, err = proc.communicate(input=json.dumps(req) + "\n", timeout=5)
    
    if proc.returncode != 0 and not out.strip():
        print(f"Error: {err}")
        sys.exit(1)
        
    responses = [line for line in out.splitlines() if line.strip()]
    if not responses:
        print("No response from mindsync")
        sys.exit(1)
        
    print("Received response:")
    print(responses[0])
    
    resp_json = json.loads(responses[0])
    if "error" in resp_json:
        print(f"Server returned error: {resp_json['error']}")
        sys.exit(1)
        
    if "result" not in resp_json or "protocolVersion" not in resp_json["result"]:
        print("Invalid initialize response")
        sys.exit(1)
        
    print("MCP stdio test passed.")
    sys.exit(0)

if __name__ == "__main__":
    main()
