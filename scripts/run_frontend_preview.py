"""Run the real API with isolated preview data and no model-generated greeting."""
import argparse
import os
import sys
from pathlib import Path

def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--data-dir", type=Path, default=root / "_tmptest" / "frontend-preview")
    args = parser.parse_args()
    sys.path.insert(0, str(root))
    os.environ["DATA_DIR"] = str(args.data_dir.resolve())
    os.environ["WB_AMAP_KEY"] = ""
    os.environ["WB_AUTH_REQUIRED"] = "0"
    from app import config
    import uvicorn
    config.GREETING_USE_MEMORY = False
    uvicorn.run("app.main:app", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
