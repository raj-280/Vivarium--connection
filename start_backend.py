import os
import sys
from pathlib import Path

# Use vivarium.db as the main database (setdefault respects any value already
# set in the environment, so you can still override with $env:DATABASE_URL=...)
os.environ.setdefault("DATABASE_URL", "sqlite:///./vivarium.db")

# Add server directory to sys.path (so 'from main import app' works)
server_path = str(Path("./server").resolve())
if server_path not in sys.path:
    sys.path.insert(0, server_path)

import uvicorn
from main import app  # server/ is on path, so 'main' resolves to server/main.py

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
