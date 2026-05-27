"""
Mastodon OAuth authorization-code flow for desktop apps.

Opens the user's browser to the instance's authorization page.
A local HTTP server on a random port captures the redirect callback,
exchanges the code for a token, and verifies the account.
Runs entirely on a daemon thread — all callbacks fire from that thread,
so UI callers must use wx.CallAfter.
"""

import http.server
import socketserver
import threading
import urllib.parse
import webbrowser
from typing import Callable

import httpx

APP_NAME    = "Pub-Streamer"
APP_WEBSITE = "https://github.com/pub-streamer/pub-streamer"
SCOPES      = "read write"


def _normalize(instance: str) -> str:
    instance = instance.strip().rstrip("/")
    if not instance.startswith("http"):
        instance = "https://" + instance
    return instance


def run_oauth_flow(
    instance: str,
    on_success: Callable[[str, str], None],   # (access_token, acct_string)
    on_error:   Callable[[str], None],         # (error_message)
    timeout:    float = 120.0,
) -> None:
    """Start the OAuth flow on a background daemon thread."""
    threading.Thread(
        target=_worker,
        args=(instance, on_success, on_error, timeout),
        daemon=True,
        name="masto-oauth",
    ).start()


def _worker(instance: str, on_success, on_error, timeout: float) -> None:
    try:
        base = _normalize(instance)

        code_holder: list[str] = []
        code_event = threading.Event()
        server_ref: list[socketserver.TCPServer] = []

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                params = urllib.parse.parse_qs(parsed.query)
                if "code" in params:
                    code_holder.append(params["code"][0])
                body = (
                    b"<html><body style='font-family:sans-serif;padding:2em'>"
                    b"<h2>Authorization complete</h2>"
                    b"<p>You can close this tab and return to Pub-Streamer.</p>"
                    b"</body></html>"
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                code_event.set()
                # Shut down from a new thread to avoid deadlocking serve_forever.
                threading.Thread(target=server_ref[0].shutdown, daemon=True).start()

            def log_message(self, *_):
                pass

        class _Server(socketserver.TCPServer):
            allow_reuse_address = True

        with _Server(("127.0.0.1", 0), _Handler) as server:
            server_ref.append(server)
            port = server.server_address[1]
            redirect_uri = f"http://127.0.0.1:{port}/callback"

            # Register this app with the Mastodon instance.
            reg = httpx.post(
                f"{base}/api/v1/apps",
                data={
                    "client_name":   APP_NAME,
                    "redirect_uris": redirect_uri,
                    "scopes":        SCOPES,
                    "website":       APP_WEBSITE,
                },
                timeout=15.0,
            )
            reg.raise_for_status()
            reg_data      = reg.json()
            client_id     = reg_data["client_id"]
            client_secret = reg_data["client_secret"]

            # Open the browser to the authorization page.
            auth_params = urllib.parse.urlencode({
                "response_type": "code",
                "client_id":     client_id,
                "redirect_uri":  redirect_uri,
                "scope":         SCOPES,
            })
            webbrowser.open(f"{base}/oauth/authorize?{auth_params}")

            # Wait for the browser to hit the callback.
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            code_event.wait(timeout=timeout)
            if not code_event.is_set():
                threading.Thread(target=server.shutdown, daemon=True).start()
                on_error("Authorization timed out — please try again.")
                return
            t.join(timeout=5.0)

        if not code_holder:
            on_error("No authorization code received.")
            return

        # Exchange the code for an access token.
        tok = httpx.post(
            f"{base}/oauth/token",
            data={
                "client_id":     client_id,
                "client_secret": client_secret,
                "redirect_uri":  redirect_uri,
                "grant_type":    "authorization_code",
                "code":          code_holder[0],
                "scope":         SCOPES,
            },
            timeout=15.0,
        )
        tok.raise_for_status()
        token = tok.json()["access_token"]

        # Verify the token and get the account name.
        me = httpx.get(
            f"{base}/api/v1/accounts/verify_credentials",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15.0,
        )
        me.raise_for_status()
        me_data = me.json()
        acct    = me_data.get("acct", "unknown")
        if "@" not in acct:
            domain = urllib.parse.urlparse(base).netloc
            acct   = f"{acct}@{domain}"

        on_success(token, acct)

    except Exception as exc:
        on_error(str(exc))
