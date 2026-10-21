"""
Start Jinora Local Web Server
Runs FastAPI + Uvicorn server configurable via environment variables.
"""

import os
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
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    env_mode = os.getenv("ENV", "prod").lower()
    reload_enabled = env_mode == "dev"

    print("\n" + "=" * 60)
    print("🚀 INICIANDO SERVIDOR WEB JINORA (FastAPI)")
    print(f"📍 Host: {host}:{port} | Modo ENV: '{env_mode}' (reload={reload_enabled})")
    print("📊 Dashboard y Streaming de Videos activo")
    print("=" * 60 + "\n")

    uvicorn.run("src.web.app:app", host=host, port=port, reload=reload_enabled)
