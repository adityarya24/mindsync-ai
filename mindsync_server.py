import os
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from mcp.server.fastmcp import FastMCP

# Define FastMCP server
mcp = FastMCP("MindSync")

# Directory and file configurations
LOCAL_GBRAIN_DIR = Path("C:/Users/ADITYA/.local-gbrain")
STATE_FILE = LOCAL_GBRAIN_DIR / "local-state.json"
AUDIT_FILE = LOCAL_GBRAIN_DIR / "local-audit.jsonl"
OFFLINE_QUEUE_FILE = LOCAL_GBRAIN_DIR / "offline_queue.jsonl"
COMPILED_TRUTH_DIR = LOCAL_GBRAIN_DIR / "compiled-truth"

# Ensure directories exist
LOCAL_GBRAIN_DIR.mkdir(parents=True, exist_ok=True)
COMPILED_TRUTH_DIR.mkdir(parents=True, exist_ok=True)

def _load_state() -> dict:
    if not STATE_FILE.exists():
        initial_state = {
            "active_project": "unknown",
            "active_branch": "unknown",
            "custom_names": {
                "gemini-antigravity": "Satyaki",
                "codex-openai": "Abhimanyu",
                "claude-code": "Rudra",
                "grok-cli": "Ashwatthama"
            },
            "agents_focus": {}
        }
        _save_state(initial_state)
        return initial_state
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

def _log_audit(agent_name: str, action: str, details: str):
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": agent_name,
        "action": action,
        "details": details
    }
    with open(AUDIT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

def _check_vps_online() -> bool:
    try:
        # Run a quick lightweight ssh ping with a 3-second timeout
        res = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=3", "openclaw-vps", "echo 1"],
            capture_output=True,
            text=True,
            timeout=4
        )
        return res.stdout.strip() == "1"
    except Exception:
        return False

@mcp.tool()
def get_sync_context(agent_name: str, project_name: str = None) -> dict:
    """Load the consolidated local and remote sync context at start.
    
    This fulfills the offline-first requirement.
    """
    state = _load_state()
    
    # Read compiled truth markdown files
    compiled_truth = {}
    if COMPILED_TRUTH_DIR.exists():
        for path in COMPILED_TRUTH_DIR.glob("*.md"):
            try:
                compiled_truth[path.stem] = path.read_text(encoding="utf-8")
            except Exception:
                pass

    _log_audit(agent_name, "get_sync_context", f"Loaded context for project: {project_name or 'any'}")
    
    return {
        "local_state": state,
        "compiled_truth": compiled_truth,
        "vps_online": _check_vps_online()
    }

@mcp.tool()
def update_focus(agent_name: str, project: str, branch: str, focus: str) -> dict:
    """Update the agent's active focus in the shared local state.
    
    Detects if another agent is working on the same focus/files to prevent collision.
    """
    state = _load_state()
    
    # Update project and branch globally
    state["active_project"] = project
    state["active_branch"] = branch
    
    # Initialize agents_focus if missing
    if "agents_focus" not in state:
        state["agents_focus"] = {}
        
    warnings = []
    
    # Check for conflicts
    current_time = datetime.now(timezone.utc)
    for other_agent, other_data in state["agents_focus"].items():
        if other_agent == agent_name:
            continue
            
        # Parse timestamp
        try:
            other_time = datetime.fromisoformat(other_data["timestamp"])
            age_seconds = (current_time - other_time).total_seconds()
        except Exception:
            age_seconds = 999999
            
        # If active in the last 2 hours
        if age_seconds < 7200:
            other_focus = other_data["focus"].lower()
            new_focus = focus.lower()
            
            # Simple keyword matching for conflict detection (e.g. same filenames or project areas)
            conflict_words = []
            for word in new_focus.split():
                clean_word = "".join(c for c in word if c.isalnum() or c in (".", "_", "-"))
                if len(clean_word) > 4 and clean_word in other_focus:
                    conflict_words.append(clean_word)
                    
            if conflict_words or state.get("active_project") == project:
                warn_msg = (
                    f"CONFLICT WARNING: Agent '{other_agent}' is active in project '{project}' "
                    f"and focusing on '{other_data['focus']}'. Your focus '{focus}' might overlap "
                    f"on: {', '.join(conflict_words) if conflict_words else 'same project'}."
                )
                warnings.append(warn_msg)
                
    # Save the new focus
    state["agents_focus"][agent_name] = {
        "focus": focus,
        "timestamp": current_time.isoformat()
    }
    
    _save_state(state)
    _log_audit(agent_name, "update_focus", f"Focused on '{focus}' in project '{project}' (branch: '{branch}')")
    
    return {
        "ok": True,
        "warnings": warnings,
        "message": f"Focus updated successfully for {agent_name}."
    }

@mcp.tool()
def queue_durable_fact(
    agent_name: str, entity: str, attribute: str, text: str, confidence: float = 1.0
) -> dict:
    """Queue a fact for VPS storage. Writes directly to VPS if online, else queues offline.
    
    Maintains the 'offline-first, best-effort cloud' architecture.
    """
    fact = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": agent_name,
        "entity": entity,
        "attribute": attribute,
        "text": text,
        "source": f"agent:{agent_name}",
        "confidence": confidence
    }
    
    vps_online = _check_vps_online()
    if vps_online:
        # Push immediately
        try:
            # Run SSH command to push to remote DB
            escaped_text = text.replace("'", "'\\''")
            ssh_cmd = (
                f"cd /home/aditya/.openclaw/workspace/gbrain && "
                f"set -a; source config/gbrain.env; set +a; "
                f"python3 tools/gbrain_fact.py write "
                f"--agent '{agent_name}' --entity '{entity}' --attribute '{attribute}' "
                f"--text '{escaped_text}' --source 'agent:{agent_name}' --confidence {confidence}"
            )
            res = subprocess.run(
                ["ssh", "openclaw-vps", ssh_cmd],
                capture_output=True,
                text=True,
                check=True
            )
            _log_audit(agent_name, "write_fact_vps", f"Fact written to remote VPS: {entity} -> {attribute}")
            return {
                "ok": True,
                "synced_to_vps": True,
                "queued_locally": False,
                "details": res.stdout.strip()
            }
        except Exception as e:
            # Fallback to local queue if remote write fails despite ping
            pass
            
    # Queue locally
    with open(OFFLINE_QUEUE_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(fact, ensure_ascii=False) + "\n")
        
    _log_audit(agent_name, "queue_fact_local", f"VPS offline. Queued fact locally: {entity} -> {attribute}")
    
    return {
        "ok": True,
        "synced_to_vps": False,
        "queued_locally": True,
        "message": "VPS is offline. Fact has been queued locally."
    }

@mcp.tool()
def sync_offline_facts(agent_name: str) -> dict:
    """Flush queued offline facts to the VPS database when back online.
    
    Consolidates the remote database and pulls fresh compiled truths down.
    """
    if not OFFLINE_QUEUE_FILE.exists() or os.path.getsize(OFFLINE_QUEUE_FILE) == 0:
        return {"ok": True, "message": "No offline facts in the queue."}
        
    vps_online = _check_vps_online()
    if not vps_online:
        return {"ok": False, "message": "Cannot sync: VPS is still offline."}
        
    # Read queued facts
    try:
        facts = []
        with open(OFFLINE_QUEUE_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    facts.append(json.loads(line.strip()))
    except Exception as e:
        return {"ok": False, "message": f"Failed to read offline queue: {e}"}
        
    success_count = 0
    failed_facts = []
    
    for fact in facts:
        try:
            escaped_text = fact["text"].replace("'", "'\\''")
            ssh_cmd = (
                f"cd /home/aditya/.openclaw/workspace/gbrain && "
                f"set -a; source config/gbrain.env; set +a; "
                f"python3 tools/gbrain_fact.py write "
                f"--agent '{fact['agent']}' --entity '{fact['entity']}' --attribute '{fact['attribute']}' "
                f"--text '{escaped_text}' --source '{fact['source']}' --confidence {fact['confidence']}"
            )
            subprocess.run(["ssh", "openclaw-vps", ssh_cmd], check=True, capture_output=True)
            success_count += 1
        except Exception:
            failed_facts.append(fact)
            
    # Rewrite queue file with failed facts (or empty it if all succeeded)
    if failed_facts:
        with open(OFFLINE_QUEUE_FILE, "w", encoding="utf-8") as f:
            for fact in failed_facts:
                f.write(json.dumps(fact, ensure_ascii=False) + "\n")
    else:
        OFFLINE_QUEUE_FILE.write_text("", encoding="utf-8")
        
    # Run consolidation and pull updated truth
    pull_success = False
    try:
        # Consolidate
        subprocess.run(
            ["ssh", "openclaw-vps", "cd /home/aditya/.openclaw/workspace/gbrain && set -a; source config/gbrain.env; set +a; python3 tools/gbrain_consolidate.py"],
            check=True,
            capture_output=True
        )
        # Pull
        subprocess.run(
            ["scp", "-r", "openclaw-vps:/home/aditya/.openclaw/workspace/gbrain/compiled-truth/*", f"{COMPILED_TRUTH_DIR}/"],
            check=True,
            capture_output=True
        )
        pull_success = True
    except Exception as e:
        pass
        
    _log_audit(
        agent_name,
        "sync_offline_facts",
        f"Synced {success_count} facts (remaining queue: {len(failed_facts)}). Pull success: {pull_success}"
    )
    
    return {
        "ok": len(failed_facts) == 0,
        "synced_count": success_count,
        "remaining_queue": len(failed_facts),
        "pull_success": pull_success,
        "message": f"Successfully synced {success_count} facts to VPS."
    }

if __name__ == "__main__":
    mcp.run()
