"""Google sign-in for Calendar and Gmail.

The first time a Google tool is used, a browser window opens for the owner to approve
access. The resulting login token is saved in data/google_token.json (git-ignored) and
refreshed automatically. While the Google app is in "testing" mode, Google expires it
about weekly; the browser approval then simply runs again.
"""
from __future__ import annotations

import threading
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/calendar",        # read and change calendar events
    "https://www.googleapis.com/auth/gmail.modify",    # read, label, draft, send, trash email (no permanent delete)
    "https://www.googleapis.com/auth/drive",           # all Drive files, incl. Docs, Sheets, Slides
    "https://www.googleapis.com/auth/contacts",        # read and add contacts
    "https://www.googleapis.com/auth/tasks",           # Google Tasks
    "https://www.googleapis.com/auth/youtube.force-ssl",  # YouTube channel, videos, comments
]
SETUP_HELP = (
    "Google isn't set up. The owner needs a Google Cloud 'Desktop app' OAuth client file saved as "
    "{path}, with the Calendar, Gmail, Drive, People, Tasks and YouTube Data APIs enabled and their "
    "Gmail added as a test user."
)


class GoogleNotReady(Exception):
    pass


class GoogleAuth:
    def __init__(self, credentials_path: Path, token_path: Path):
        self.credentials_path = credentials_path
        self.token_path = token_path
        self._lock = threading.Lock()
        self._creds: Credentials | None = None
        self._services: dict[str, object] = {}

    def _login(self) -> Credentials:
        if not self.credentials_path.exists():
            raise GoogleNotReady(SETUP_HELP.format(path=self.credentials_path))
        print("\n[Google] A browser window is opening. Approve access there (waiting up to 3 minutes)...\n", flush=True)
        import json
        from . import vault
        flow = InstalledAppFlow.from_client_config(json.loads(vault.read_text(self.credentials_path)), SCOPES)
        try:
            creds = flow.run_local_server(
                port=0, open_browser=True, timeout_seconds=180,
                authorization_prompt_message="[Google] If no browser opened, visit: {url}",
                success_message="Fafnir is connected to Google. You can close this tab.",
            )
        except Exception as e:
            raise GoogleNotReady(f"Google sign-in didn't complete ({type(e).__name__}). Try again and approve in the browser.")
        if creds is None:
            raise GoogleNotReady("Google sign-in timed out. Try again and approve in the browser.")
        return creds

    def credentials(self) -> Credentials:
        with self._lock:
            creds = self._creds
            if creds is None and self.token_path.exists():
                try:
                    import json
                    from . import vault
                    creds = Credentials.from_authorized_user_info(json.loads(vault.read_text(self.token_path)), SCOPES)
                except Exception:
                    creds = None
            if creds is not None and not creds.valid:
                if creds.expired and creds.refresh_token:
                    try:
                        creds.refresh(Request())
                    except RefreshError:
                        creds = None  # expired for good (weekly in testing mode): sign in again
                else:
                    creds = None
            if creds is None or not set(SCOPES) <= set(creds.scopes or SCOPES):
                creds = self._login()
                self._services.clear()
            self.token_path.parent.mkdir(parents=True, exist_ok=True)
            from . import vault
            vault.write_text(self.token_path, creds.to_json())   # the login token, encrypted
            self._creds = creds
            return creds

    @property
    def signed_in(self) -> bool:
        """True if a login token exists (so a lookup won't pop up a browser sign-in)."""
        return self._creds is not None or self.token_path.exists()

    def service(self, name: str, version: str):
        """A Google API client for the calling thread. Google's HTTP library (httplib2)
        isn't thread-safe, and the app uses Google from several threads at once (tools,
        side panels, heartbeat), so each thread gets its own client."""
        creds = self.credentials()
        key = (threading.get_ident(), name, version)
        with self._lock:
            if key not in self._services:
                self._services[key] = build(name, version, credentials=creds, cache_discovery=False)
            return self._services[key]
