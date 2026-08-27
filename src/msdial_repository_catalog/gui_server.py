from __future__ import annotations

import argparse
import json
import mimetypes
import threading
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any

from .mcp_server import DEFAULT_DATABASE
from .storage import Catalog


class CatalogGuiApplication:
    def __init__(self, database: str | Path) -> None:
        self.database = Path(database).expanduser().resolve()

    def status(self) -> dict[str, Any]:
        with Catalog(self.database) as catalog:
            return catalog.overview()

    def search(self, query: dict[str, str]) -> dict[str, Any]:
        maximum_gb = _optional_float(query.get("max_download_gb", ""))
        limit = _bounded_int(query.get("limit", "100"), default=100, minimum=1, maximum=500)
        with Catalog(self.database) as catalog:
            matches = catalog.search(
                text=query.get("text", "").strip(),
                repository=query.get("repository", "").strip(),
                separation=query.get("separation", "").strip(),
                chromatography=query.get("chromatography", "").strip(),
                ion_mode=query.get("ion_mode", "").strip(),
                acquisition_mode=query.get("acquisition_mode", "").strip(),
                target_omics=query.get("target_omics", "").strip(),
                biological_context=query.get("biological_context", "").strip(),
                review_status=query.get("review_status", "").strip(),
                max_download_bytes=(int(maximum_gb * 1024**3) if maximum_gb is not None else None),
                limit=limit,
            )
        return {"count": len(matches), "matches": matches}

    def unit(self, unit_id: str) -> dict[str, Any]:
        with Catalog(self.database) as catalog:
            return catalog.get_unit(unit_id)


class CatalogGuiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], application: CatalogGuiApplication) -> None:
        super().__init__(address, CatalogGuiRequestHandler)
        self.application = application


class CatalogGuiRequestHandler(BaseHTTPRequestHandler):
    server: CatalogGuiServer

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path == "/api/status":
                self._json(self.server.application.status())
                return
            if parsed.path == "/api/search":
                raw = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                query = {key: values[-1] for key, values in raw.items()}
                self._json(self.server.application.search(query))
                return
            if parsed.path.startswith("/api/unit/"):
                unit_id = urllib.parse.unquote(parsed.path.removeprefix("/api/unit/")).strip()
                if not unit_id:
                    raise ValueError("Analysis unit ID is required.")
                self._json(self.server.application.unit(unit_id))
                return
            if parsed.path == "/api/health":
                self._json({"status": "ok"})
                return
            self._static(parsed.path)
        except KeyError as error:
            self._json({"error": str(error)}, HTTPStatus.NOT_FOUND)
        except (ValueError, OSError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except Exception as error:
            self._json({"error": f"Catalog GUI request failed: {error}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _static(self, path: str) -> None:
        asset = {
            "/": "index.html",
            "/index.html": "index.html",
            "/app.js": "app.js",
            "/styles.css": "styles.css",
        }.get(path)
        if asset is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        content = files("msdial_repository_catalog.gui").joinpath(asset).read_bytes()
        mime = mimetypes.guess_type(asset)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(content)

    def _json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        content = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(content)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'",
        )

    def log_message(self, format: str, *args: object) -> None:
        print(f"[catalog-gui] {self.address_string()} {format % args}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Browse the local MS-DIAL Repository Catalog")
    parser.add_argument("--database", default=str(DEFAULT_DATABASE))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    application = CatalogGuiApplication(args.database)
    application.status()  # Initialize and validate the database before opening a browser.
    server = CatalogGuiServer((args.host, args.port), application)
    host, port = server.server_address[:2]
    url_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"http://{url_host}:{port}/"
    print("MS-DIAL Repository Catalog GUI")
    print(f"Database: {application.database}")
    print(f"URL: {url}")
    print("Keep this terminal open while the GUI is in use. Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping catalog GUI.")
    finally:
        server.server_close()
    return 0


def _optional_float(value: str) -> float | None:
    if not str(value or "").strip():
        return None
    result = float(value)
    if result < 0:
        raise ValueError("Maximum download size must be zero or greater.")
    return result


def _bounded_int(value: str, *, default: int, minimum: int, maximum: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    return max(minimum, min(result, maximum))


if __name__ == "__main__":
    raise SystemExit(main())
