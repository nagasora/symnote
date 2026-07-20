import sys
from unittest.mock import MagicMock

# Mock google.generativeai
mock_genai = MagicMock()
sys.modules["google"] = MagicMock()
sys.modules["google.generativeai"] = mock_genai

from symnote.config import load_config
from symnote.core.db import init_db, get_connection
from symnote.core.mindmap import create_node, get_nodes_by_project

# Try importing nlp now
try:
    from symnote.core.nlp import generate_initial_mindmap
    print("Successfully imported generate_initial_mindmap (with mocked genai)")
except Exception as e:
    print(f"Failed to import generate_initial_mindmap: {e}")

def test_db_table():
    print("\n--- Checking Table ---")
    init_db() # Ensure table exists
    conn = get_connection()
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='mindmap_nodes'")
    row = cur.fetchone()
    if row:
        print("Table 'mindmap_nodes' exists.")
    else:
        print("Table 'mindmap_nodes' DOES NOT EXIST.")

def test_insertion():
    print("\n--- Testing Insertion ---")
    project_id = 9999
    try:
        node_id = create_node(project_id, "root", "Test Root", "Body")
        print(f"Created node {node_id}")
        nodes = get_nodes_by_project(project_id)
        print(f"Retrieved {len(nodes)} nodes for project {project_id}")
        for n in nodes:
            print(f" - {n.title} ({n.kind})")
    except Exception as e:
        print(f"Insertion failed: {e}")

def test_generation():
    print("\n--- Testing Generation (Mock) ---")
    # We won't call actual LLM here to save time/tokens unless needed, 
    # but we can simulate what app.py does.
    
    fake_data = {
        "title": "Fake Project",
        "body": "Fake Summary",
        "children": [
            { "kind": "idea", "title": "Fake Idea", "body": "...", "children": [] }
        ]
    }
    
    try:
        # Simulate _save_mindmap_recursive logic
        print("Simulating save...")
        # Copy-paste logic from app.py roughly
        def _save_recursive(pid, data, parent=None):
            kind = data.get("kind", "root")
            title = data.get("title", "No Title")
            body = data.get("body", "")
            nid = create_node(pid, kind, title, body, parent)
            print(f"Saved {kind}: {title} (ID: {nid})")
            for child in data.get("children", []):
                _save_recursive(pid, child, nid)
                
        _save_recursive(9999, fake_data)
        print("Save simulation complete.")
    except Exception as e:
        print(f"Save simulation failed: {e}")

if __name__ == "__main__":
    # Check DB content
    conn = get_connection()
    print("\n--- Checking Mindmap Nodes ---")
    try:
        cur = conn.execute("SELECT * FROM mindmap_nodes ORDER BY id DESC LIMIT 20")
        rows = cur.fetchall()
        if not rows:
            print("No nodes found in mindmap_nodes.")
        else:
            for r in rows:
                print(f"ID: {r['id']}, Project: {r['project_id']}, Kind: {r['kind']}, Title: {r['title']}")
    except Exception as e:
        print(f"Error querying nodes: {e}")

    print("\n--- Checking Sessions ---")
    try:
        cur = conn.execute("SELECT * FROM idea_sessions ORDER BY id DESC LIMIT 5")
        rows = cur.fetchall()
        for r in rows:
            print(f"Session ID: {r['id']}, Title: {r['title']}")
    except Exception as e:
        print(f"Error querying sessions: {e}")
