"""Lightweight HTTP API for serving data files from the droplet.
Runs alongside the trading agent, exposes JSON data on port 9090.
The local dashboard fetches from this instead of using SCP/SSH.
"""
import json
import os
import threading
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler

logger = logging.getLogger(__name__)

DATA_DIR = "data"
PORT = 9090


class DataAPIHandler(BaseHTTPRequestHandler):
    """Serves data files as JSON over HTTP."""

    ALLOWED_FILES = [
        "trading_budget.json",
        "trade_history.json",
        "scorecard.json",
        "review_log.json",
        "cycle_logs.json",
        "agent_notes.json",
        "portfolio_state.json",
    ]

    def do_GET(self):
        path = self.path.strip("/")

        if path == "health":
            # Include last cycle time so dashboard can detect stale agent
            last_cycle_time = None
            cycle_file = os.path.join(DATA_DIR, "cycle_logs.json")
            if os.path.exists(cycle_file):
                try:
                    with open(cycle_file, "r") as f:
                        cycles = json.load(f)
                    if cycles:
                        last_cycle_time = cycles[-1].get("start")
                except Exception:
                    pass
            self._respond(200, {"status": "ok", "last_cycle": last_cycle_time})
            return

        if path == "all":
            # Return all data files in one response
            data = {}
            for fname in self.ALLOWED_FILES:
                filepath = os.path.join(DATA_DIR, fname)
                if os.path.exists(filepath):
                    try:
                        with open(filepath, "r") as f:
                            data[fname.replace(".json", "")] = json.load(f)
                    except Exception:
                        data[fname.replace(".json", "")] = None
            self._respond(200, data)
            return

        # Serve individual files
        if path in self.ALLOWED_FILES:
            filepath = os.path.join(DATA_DIR, path)
            if os.path.exists(filepath):
                with open(filepath, "r") as f:
                    self._respond(200, json.load(f))
            else:
                self._respond(404, {"error": "file not found"})
            return

        self._respond(404, {"error": "not found", "available": self.ALLOWED_FILES})

    def do_POST(self):
        path = self.path.strip("/")

        # Accept budget updates via POST
        if path == "trading_budget.json":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                data = json.loads(body)
                filepath = os.path.join(DATA_DIR, "trading_budget.json")
                with open(filepath, "w") as f:
                    json.dump(data, f, indent=2)
                self._respond(200, {"ok": True, "written": "trading_budget.json"})
            except Exception as e:
                self._respond(400, {"error": str(e)})
            return

        self._respond(405, {"error": "method not allowed"})

    def _respond(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(json.dumps(data, default=str).encode())
        except BrokenPipeError:
            pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except BrokenPipeError:
            pass

    def log_message(self, format, *args):
        pass  # Suppress request logs


def start_data_api():
    """Start the data API server in a background thread."""
    try:
        server = HTTPServer(("0.0.0.0", PORT), DataAPIHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info(f"Data API started on port {PORT}")
    except Exception as e:
        logger.warning(f"Failed to start data API: {e}")
