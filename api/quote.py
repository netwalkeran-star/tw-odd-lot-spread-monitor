"""
Vercel Python Serverless Function for /api/quote
Reuses the core fetching logic from server.py (zero third-party dependencies).
"""

import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler

# Allow importing server.py from project root
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from server import query_stocks  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        raw_codes = qs.get("codes", [""])[0]
        mode = qs.get("mode", ["auto"])[0]
        codes = [c.strip() for c in raw_codes.replace(" ", ",").split(",") if c.strip()]

        try:
            res = query_stocks(codes, force_mode=mode)
            status = 200
        except Exception as e:
            res = {
                "mode": "error",
                "mode_label": "伺服器錯誤",
                "error": str(e),
                "items": [],
            }
            status = 500

        body = json.dumps(res, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
