"""Run Carrot against a throwaway data directory, for looking at the UI.

Never touches the real database: `CARROT_DATA_DIR` is redirected to a temp
folder before anything imports config, so toggling a setting while checking a
screen cannot change what the user actually has configured.

    python scripts/dev_preview.py [port]
"""
import os
import sys
import tempfile

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8199

data_dir = os.path.join(tempfile.gettempdir(), f"carrot-preview-{PORT}")
os.makedirs(data_dir, exist_ok=True)
os.environ["CARROT_DATA_DIR"] = data_dir

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from carrot.database import init_db  # noqa: E402
from carrot.app import app  # noqa: E402

if __name__ == "__main__":
    import uvicorn

    init_db()
    print(f"preview data dir: {data_dir}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
