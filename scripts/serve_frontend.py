"""Static dev server for frontend/ that disables browser caching.

Usage:  python scripts/serve_frontend.py [port]   (default 5173)
"""

import functools
import http.server
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "frontend"


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5173
    handler = functools.partial(NoCacheHandler, directory=str(ROOT))
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        print(f"Serving {ROOT} at http://localhost:{port}")
        httpd.serve_forever()
