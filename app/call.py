"""Trigger a test call:  python app\\call.py "test message" [number]"""
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

cfg = config.load()
body = {"message": sys.argv[1] if len(sys.argv) > 1 else "Test call"}
if len(sys.argv) > 2:
    body["number"] = sys.argv[2]
req = urllib.request.Request(
    f"http://127.0.0.1:{cfg.http_port}/call",
    data=json.dumps(body).encode(),
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {cfg.api_token}"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        result = f"{r.status} {r.read().decode()}"
except urllib.error.HTTPError as e:
    result = f"{e.code} {e.read().decode()}"
except Exception as e:  # noqa: BLE001
    result = f"ERROR {e!r}"
print(result)
logdir = Path(cfg.log_dir)
logdir.mkdir(parents=True, exist_ok=True)
(logdir / "last-trigger.txt").write_text(result, encoding="utf-8")
