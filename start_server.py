"""
Start Jinora Local Web Server
Runs FastAPI + Uvicorn server on http://localhost:8000
"""

import sys
from pathlib import Path

# Ensure UTF-8 output on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import uvicorn

# Ensure Jinora root is in sys.path
BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("🚀 INICIANDO SERVIDOR WEB JINORA (FastAPI)")
    print("📍 URL Local: http://localhost:8000")
    print("📊 Dashboard y Streaming de Videos activo")
    print("=" * 60 + "\n")
    uvicorn.run("src.web.app:app", host="127.0.0.1", port=8000, reload=True)
