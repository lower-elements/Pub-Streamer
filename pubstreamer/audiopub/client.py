"""
Audio Pub account client.

Handles login, profile fetch, stream creation, and stream termination
against the Audio Pub SvelteKit backend.  All network methods are
synchronous and intended to be called from background threads.
"""

import base64
import json
import threading
from typing import Callable

import httpx


def _decode_jwt(token: str) -> dict:
    """Base64-decode the payload of a JWT without signature verification."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return {}


def _extract_profile(nodes: list) -> dict:
    """
    Pull displayName and streamKey out of a SvelteKit __data.json devalue
    response.  The devalue format stores primitives in a flat data array;
    data[0] is the root object whose values are indices into that array.
    """
    result: dict = {}
    for node in nodes:
        if node.get("type") != "data":
            continue
        items = node.get("data", [])
        if not isinstance(items, list) or not items:
            continue
        root = items[0]
        if not isinstance(root, dict):
            continue
        for field in ("streamKey", "displayName", "name"):
            idx = root.get(field)
            if isinstance(idx, int) and idx < len(items):
                val = items[idx]
                if isinstance(val, str):
                    result[field] = val
        if result:
            break
    return result


class AudioPubClient:
    """
    Thin wrapper around the Audio Pub web app's form actions and JSON
    endpoints.  One instance lives for the lifetime of the app.

    All state that needs to survive restarts is stored externally in
    Config; call restore() on startup if a token is already saved.
    """

    def __init__(self):
        self._base_url  = ""
        self._token     = ""         # raw JWT string
        self._lock      = threading.Lock()

        # Populated after login / restore
        self.user_id:      str = ""
        self.display_name: str = ""
        self.stream_key:   str = ""
        self.current_stream_id: str | None = None

        # Fired on the calling thread when login completes.
        self.on_login_ok:    Callable[[str, str, str], None] | None = None  # (user_id, display_name, stream_key)
        self.on_login_fail:  Callable[[str], None] | None = None            # (error_message)

    # ── setup / restore ──────────────────────────────────────────────────────

    def setup(self, base_url: str) -> None:
        """Call whenever base_url changes (e.g. when config is loaded)."""
        self._base_url = base_url.rstrip("/")

    def restore(self, token: str, user_id: str,
                display_name: str, stream_key: str) -> None:
        """
        Restore a saved session without a network call.
        Called at startup if a token is already stored in config.
        """
        self._token       = token
        self.user_id      = user_id
        self.display_name = display_name
        self.stream_key   = stream_key

    # ── public API ───────────────────────────────────────────────────────────

    @property
    def is_logged_in(self) -> bool:
        return bool(self._token and self.user_id)

    def get_token(self) -> str:
        return self._token

    def login_async(self, email: str, password: str) -> None:
        """
        Start a background login attempt.
        Fires on_login_ok(user_id, display_name, stream_key) or
        on_login_fail(message) when complete.
        """
        threading.Thread(
            target=self._login_worker,
            args=(email.strip(), password),
            daemon=True,
            name="ap-login",
        ).start()

    def create_stream(self, title: str,
                      description: str = "",
                      should_archive: bool = False) -> str:
        """
        Create a stream record on Audio Pub.
        Returns the stream UUID on success.
        Raises RuntimeError on failure.
        Must be called from a background thread.
        """
        if not self.is_logged_in:
            raise RuntimeError("Not logged in")
        data: dict = {"title": title, "description": description}
        if should_archive:
            data["shouldArchive"] = "on"
        resp = self._post("/live/new", data=data)

        # Resolve the redirect location from either HTTP 303 or SvelteKit JSON.
        location = ""
        ct = resp.headers.get("content-type", "")
        if resp.status_code == 303:
            location = resp.headers.get("location", "")
        elif resp.status_code == 200 and "application/json" in ct:
            body = resp.json()
            if body.get("type") == "redirect":
                location = body.get("location", "")
            else:
                raise RuntimeError(
                    self._parse_sv_failure(body)
                    or f"Stream creation failed: {resp.text[:200]}"
                )

        stream_id = location.rstrip("/").rsplit("/", 1)[-1]
        if stream_id and stream_id not in ("", "new"):
            self.current_stream_id = stream_id
            return stream_id
        raise RuntimeError(
            f"Stream creation failed (HTTP {resp.status_code}): "
            + resp.text[:200]
        )

    def end_stream(self, stream_id: str | None = None) -> None:
        """
        End a stream.  Uses current_stream_id if stream_id is not given.
        Silently ignores errors (stream may have already ended).
        Must be called from a background thread.
        """
        sid = stream_id or self.current_stream_id
        if not sid or not self.is_logged_in:
            return
        try:
            self._delete(f"/live/{sid}")
        except Exception:
            pass
        if sid == self.current_stream_id:
            self.current_stream_id = None

    def logout(self) -> None:
        self._token            = ""
        self.user_id           = ""
        self.display_name      = ""
        self.stream_key        = ""
        self.current_stream_id = None

    # ── private helpers ──────────────────────────────────────────────────────

    def send_chat(self, stream_id: str, content: str) -> None:
        """
        Post a chat message to the active stream.
        Raises RuntimeError on failure.  Call from a background thread.
        """
        if not self.is_logged_in:
            raise RuntimeError("Not logged in")
        resp = self._post_json(f"/live/{stream_id}", {"content": content})
        if resp.status_code != 200:
            try:
                msg = resp.json().get("message", "") or resp.text[:120]
            except Exception:
                msg = resp.text[:120]
            raise RuntimeError(msg or f"HTTP {resp.status_code}")

    # ── private helpers ──────────────────────────────────────────────────────

    def _headers(self) -> dict:
        h: dict = {"Origin": self._base_url}
        if self._token:
            h["Cookie"] = f"token={self._token}"
        return h

    def _post(self, path: str, data: dict) -> httpx.Response:
        return httpx.post(
            self._base_url + path,
            data=data,
            headers=self._headers(),
            follow_redirects=False,
            timeout=15.0,
        )

    def _post_json(self, path: str, data: dict) -> httpx.Response:
        return httpx.post(
            self._base_url + path,
            json=data,
            headers=self._headers(),
            follow_redirects=False,
            timeout=10.0,
        )

    def _delete(self, path: str) -> httpx.Response:
        return httpx.delete(
            self._base_url + path,
            headers=self._headers(),
            timeout=15.0,
        )

    def _get(self, path: str) -> httpx.Response:
        return httpx.get(
            self._base_url + path,
            headers=self._headers(),
            timeout=15.0,
        )

    @staticmethod
    def _parse_sv_failure(body: dict) -> str:
        """Extract the human-readable message from a SvelteKit failure JSON body."""
        try:
            raw = body.get("data", "[]")
            data = json.loads(raw) if isinstance(raw, str) else raw
            root = data[0] if data else {}
            idx = root.get("message")
            if isinstance(idx, int) and idx < len(data):
                return str(data[idx])
        except Exception:
            pass
        return ""

    def _login_worker(self, email: str, password: str) -> None:
        try:
            resp = self._post("/login", data={"email": email, "password": password})

            # SvelteKit enhanced-form protocol: the server always returns HTTP 200
            # with a JSON body whose "type" field is "redirect" (success) or
            # "failure" (bad credentials).  A plain HTTP 303 is also accepted as
            # a success path for non-JS environments.
            ct = resp.headers.get("content-type", "")
            if resp.status_code == 200 and "application/json" in ct:
                body = resp.json()
                if body.get("type") != "redirect":
                    msg = (self._parse_sv_failure(body)
                           or f"Invalid email or password (status {body.get('status', '?')})")
                    if self.on_login_fail:
                        self.on_login_fail(msg)
                    return
                # type == "redirect" → fall through to token extraction below
            elif resp.status_code not in (200, 303):
                if self.on_login_fail:
                    self.on_login_fail(
                        f"Unexpected response (HTTP {resp.status_code}) — "
                        f"check the Instance URL"
                    )
                return

            # Extract JWT from Set-Cookie header.
            token = resp.cookies.get("token") or ""
            if not token:
                for part in resp.headers.get("set-cookie", "").split(";"):
                    part = part.strip()
                    if part.startswith("token="):
                        token = part[6:]
                        break

            if not token:
                if self.on_login_fail:
                    self.on_login_fail("Login succeeded but no token received")
                return

            self._token = token
            payload     = _decode_jwt(token)
            self.user_id = payload.get("id", "")

            # Fetch profile to get displayName and streamKey.
            display_name = ""
            stream_key   = ""
            try:
                prof_resp = self._get("/profile/__data.json")
                if prof_resp.status_code == 200:
                    fields = _extract_profile(prof_resp.json().get("nodes", []))
                    display_name = (fields.get("displayName")
                                    or fields.get("name", ""))
                    stream_key   = fields.get("streamKey", "")
            except Exception as e:
                print(f"[AudioPub] profile fetch error: {e}", flush=True)

            self.display_name = display_name
            self.stream_key   = stream_key

            if self.on_login_ok:
                self.on_login_ok(self.user_id, self.display_name, self.stream_key)

        except Exception as exc:
            if self.on_login_fail:
                self.on_login_fail(str(exc))
