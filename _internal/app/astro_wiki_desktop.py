from __future__ import annotations

import argparse
import os
import runpy
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any


APP_ID = "AstroPhLLMWiki"
WINDOW_TITLE = "Astro-Note AI"
_LOG_HANDLES = []


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS).resolve() / "app"  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent


def default_settings_path() -> Path:
    appdata = os.getenv("APPDATA")
    if appdata:
        return Path(appdata) / APP_ID / "local_settings.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_ID / "local_settings.json"
    return Path.home() / ".config" / APP_ID / "local_settings.json"


def ensure_standard_streams(root: Path) -> None:
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "desktop.log"
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is not None and not getattr(stream, "closed", False) and hasattr(stream, "write"):
            continue
        handle = log_path.open("a", encoding="utf-8", buffering=1)
        _LOG_HANDLES.append(handle)
        setattr(sys, name, handle)


def configure_runtime(root: Path) -> None:
    os.environ.setdefault("ASTRO_WIKI_PROJECT_ROOT", str(root))
    os.environ.setdefault("ASTRO_WIKI_LOCAL_SETTINGS_PATH", str(default_settings_path()))
    ensure_standard_streams(root)
    if getattr(sys, "frozen", False):
        os.environ.setdefault("ASTRO_WIKI_SCRIPT_RUNNER", sys.executable)
    for candidate in (root / "scripts", root / "src"):
        text = str(candidate)
        if text not in sys.path:
            sys.path.insert(0, text)


def run_script(root: Path, script_path: str, script_args: list[str]) -> int:
    script = Path(script_path)
    if not script.is_absolute():
        script = root / script
    script = script.resolve()
    script.relative_to(root.resolve())
    if not script.exists():
        raise SystemExit(f"Script not found: {script_path}")
    old_argv = sys.argv[:]
    sys.argv = [str(script), *script_args]
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return 1
    finally:
        sys.argv = old_argv
    return 0


def wait_for_url(url: str, timeout_seconds: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                if response.status < 500:
                    return
        except Exception as exc:
            last_error = exc
            time.sleep(0.2)
    raise RuntimeError(f"Timed out waiting for {url}: {last_error}")


def create_server(host: str, port: int) -> tuple[Any, str]:
    import ui_server

    selected_port = ui_server.find_port(port)
    ui_server.ThreadingHTTPServer.allow_reuse_address = True
    server = ui_server.ThreadingHTTPServer((host, selected_port), ui_server.UiHandler)
    url = f"http://{host}:{selected_port}/app"
    return server, url


class DesktopApi:
    def __init__(self) -> None:
        self._window: Any | None = None

    def choose_folder(self) -> str:
        if self._window is None:
            return ""
        try:
            import webview

            dialog_type = getattr(getattr(webview, "FileDialog", object), "FOLDER", None)
            if dialog_type is None:
                dialog_type = webview.FOLDER_DIALOG
            selected = self._window.create_file_dialog(dialog_type, allow_multiple=False)
        except Exception as exc:
            print(f"Folder picker failed: {exc}", file=sys.stderr)
            return ""
        if not selected:
            return ""
        return str(selected[0])


def run_smoke_test(host: str, port: int) -> int:
    server, url = create_server(host, port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        wait_for_url(url)
        print(f"Smoke test OK: {url}")
        return 0
    finally:
        server.shutdown()
        server.server_close()


def serve(host: str, port: int, *, open_browser: bool) -> int:
    server, url = create_server(host, port)
    print(f"{WINDOW_TITLE} UI: {url}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def open_app_window(host: str, port: int) -> int:
    try:
        import webview
    except Exception as exc:
        print(f"Desktop app window runtime is unavailable: {exc}", file=sys.stderr)
        print("Install pywebview or run with --browser.", file=sys.stderr)
        return 1

    server, url = create_server(host, port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    api = DesktopApi()
    try:
        wait_for_url(url)
        print(f"{WINDOW_TITLE} UI: {url}")
        window = webview.create_window(
            WINDOW_TITLE,
            url,
            js_api=api,
            width=1280,
            height=860,
            min_size=(960, 640),
            text_select=True,
        )
        api._window = window
        webview.start()
        return 0
    finally:
        server.shutdown()
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root = app_root()
    configure_runtime(root)

    if argv and argv[0] == "--run-script":
        if len(argv) < 2:
            raise SystemExit("--run-script requires a script path")
        return run_script(root, argv[1], argv[2:])
    if argv and argv[0].endswith(".py"):
        return run_script(root, argv[0], argv[1:])

    parser = argparse.ArgumentParser(description="Run the Astro-Note AI desktop package.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--browser", action="store_true", help="Open the UI in the default web browser.")
    parser.add_argument("--server-only", action="store_true", help="Start only the local UI server.")
    parser.add_argument("--smoke-test", action="store_true", help="Start the app briefly and verify the UI responds.")
    args = parser.parse_args(argv)

    if args.smoke_test:
        return run_smoke_test(args.host, args.port)
    if args.server_only or args.browser:
        return serve(args.host, args.port, open_browser=args.browser)
    return open_app_window(args.host, args.port)


if __name__ == "__main__":
    raise SystemExit(main())
