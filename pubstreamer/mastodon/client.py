"""Mastodon API client — post to Mastodon and track the current stream post."""

from typing import Callable


class MastodonClient:
    """
    Shared object owned by the app, referenced by MastodonPanel,
    StreamPanel, and MastodonRepliesCapture.

    stream_url is set when Audio Pub confirms the stream is live.
    current_status_id is set when a post is successfully made; it is
    cleared when the stream stops so the Replies capture goes idle.
    """

    def __init__(self):
        self.instance_url:      str       = ""
        self.access_token:      str       = ""
        self.stream_url:         str | None = None   # set on stream-live
        self.stream_title:       str        = ""
        self.stream_description: str        = ""
        self.current_status_id:  str | None = None   # set after posting
        self.current_status_url:str | None = None

        # Fired (on a background thread) after a post succeeds.
        self.on_post_made: Callable[[str, str], None] | None = None  # (status_id, status_url)

    # ── posting ──────────────────────────────────────────────────────────────

    def post(self, text: str) -> tuple[str, str]:
        """
        Substitute {url} in *text*, post to Mastodon, store the result.
        Returns (status_id, status_url).  Raises on HTTP error.
        """
        final_text = (text
                      .replace("{url}",         self.stream_url or "")
                      .replace("{title}",       self.stream_title)
                      .replace("{description}", self.stream_description))
        import httpx
        resp = httpx.post(
            f"https://{self.instance_url.strip('/')}/api/v1/statuses",
            headers={"Authorization": f"Bearer {self.access_token}"},
            data={"status": final_text},
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
        self.current_status_id  = data["id"]
        self.current_status_url = data.get("url", "")
        if self.on_post_made:
            self.on_post_made(self.current_status_id, self.current_status_url)
        return self.current_status_id, self.current_status_url

    def clear(self):
        """Call when the stream stops so the Replies capture goes idle."""
        self.stream_url         = None
        self.stream_title       = ""
        self.stream_description = ""
        self.current_status_id  = None
        self.current_status_url = None
