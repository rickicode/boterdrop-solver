"""Non-interactive launcher for Boterdrop Solver (daemon / systemd)."""
import json, os, sys, uvicorn

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

DEFAULTS = {
    "headless": True,
    "thread": 2,
    "page_count": 1,
    "proxy_support": False,
    "proxy_file": "proxies.txt",
    "host": "0.0.0.0",
    "port": 8001,
    "debug": False,
    "cleanup_interval_minutes": 5,
}

try:
    with open(CONFIG_PATH) as f:
        cfg = {**DEFAULTS, **json.load(f)}
except Exception as e:
    print(f"[run_server] config load error: {e}, using defaults", file=sys.stderr)
    cfg = dict(DEFAULTS)

# Import after config so auto_install doesn't try interactive prompts
from api_server import create_app  # noqa: E402

app = create_app(
    headless=cfg["headless"],
    thread=cfg["thread"],
    page_count=cfg["page_count"],
    proxy_support=cfg["proxy_support"],
    proxy_file=cfg.get("proxy_file", "proxies.txt"),
    cleanup_interval_minutes=cfg.get("cleanup_interval_minutes", 5),
)

if __name__ == "__main__":
    uvicorn.run(app, host=cfg["host"], port=cfg["port"], log_level="info")
