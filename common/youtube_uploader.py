"""
Shared YouTube uploader — merged from the near-identical classes that used to
live separately in telegram_file_reciever2.py (EN) and telegram_file_reciever_ru2.py (RU).

Authentication is headless-safe: __init__ only ever loads/refreshes a cached
token, never blocks on an interactive browser flow (that would hang forever on
a server with no local browser). When no valid token is cached, `is_authorized`
is False and callers should drive `start_manual_authorization()` /
`complete_manual_authorization()` — a copy-paste OAuth flow that works from any
browser on any device, not just one reachable from the server.
"""

import logging
import os
import pickle
import re
import time
from urllib.parse import unquote

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


class YouTubeUploader:
    SCOPES = [
        "https://www.googleapis.com/auth/youtube",
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube.force-ssl",
    ]

    def __init__(self, client_secrets_file, token_file,
                 oauth_ports=(8080, 8081, 8082, 8083, 8084),
                 category_id="22", privacy_status="private"):
        self.client_secrets_file = client_secrets_file
        self.token_file = token_file
        self.oauth_ports = oauth_ports
        self.category_id = category_id
        self.privacy_status = privacy_status
        self.youtube = None
        self._pending_flow = None
        self._authenticate()

    @property
    def is_authorized(self) -> bool:
        return self.youtube is not None

    def _authenticate(self):
        """Loads/refreshes a cached token only — never blocks on interactive auth."""
        creds = None
        if os.path.exists(self.token_file):
            with open(self.token_file, 'rb') as f:
                creds = pickle.load(f)
            logging.info("YouTube: loaded cached credentials")

        if creds and creds.expired and creds.refresh_token:
            try:
                logging.info("YouTube: refreshing token...")
                creds.refresh(Request())
                with open(self.token_file, 'wb') as f:
                    pickle.dump(creds, f)
            except Exception as e:
                logging.warning(f"YouTube ({self.token_file}): refresh failed ({e}), needs re-authorization")
                creds = None

        if not creds or not creds.valid:
            logging.warning(
                f"YouTube ({self.token_file}): no valid token — use start_manual_authorization()")
            self.youtube = None
            return

        self.youtube = build('youtube', 'v3', credentials=creds)
        logging.info("✅ YouTube API ready")

    # ── Headless-server OAuth: copy-paste flow, no local browser needed ────────
    def start_manual_authorization(self) -> str:
        """
        Returns a URL to open in ANY browser on ANY device. After granting
        consent, the browser is redirected to a localhost URL that will fail
        to load (nothing is listening there) — that's expected. The user
        copies that URL (or just the `code=` value) and passes it to
        complete_manual_authorization().
        """
        redirect_uri = f"http://localhost:{self.oauth_ports[0]}/"
        flow = Flow.from_client_secrets_file(
            self.client_secrets_file, self.SCOPES, redirect_uri=redirect_uri)
        auth_url, _ = flow.authorization_url(
            access_type="offline", prompt="consent", include_granted_scopes="true")
        self._pending_flow = flow
        return auth_url

    def complete_manual_authorization(self, code_or_url: str):
        """Exchanges the pasted code/redirect-URL for tokens and saves them."""
        if not self._pending_flow:
            raise RuntimeError("No pending authorization — call start_manual_authorization() first")
        code = self._extract_code(code_or_url)
        self._pending_flow.fetch_token(code=code)
        creds = self._pending_flow.credentials
        with open(self.token_file, 'wb') as f:
            pickle.dump(creds, f)
        self.youtube = build('youtube', 'v3', credentials=creds)
        self._pending_flow = None
        logging.info(f"✅ YouTube ({self.token_file}): manual authorization complete")

    @staticmethod
    def _extract_code(text: str) -> str:
        text = text.strip()
        m = re.search(r"[?&]code=([^&\s]+)", text)
        return unquote(m.group(1)) if m else text

    def upload_video(self, video_path, title, description, tags=None,
                      thumbnail_path=None, playlist_id=None):
        if self.youtube is None:
            return {'success': False, 'error': 'YouTube not authorized — use /youtube_auth'}
        try:
            logging.info(f"YouTube: uploading '{title}'")
            body = {
                'snippet': {
                    'title': title,
                    'description': description,
                    'tags': tags or [],
                    'categoryId': self.category_id,
                },
                'status': {
                    'privacyStatus': self.privacy_status,
                    'selfDeclaredMadeForKids': False,
                }
            }
            media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype='video/*')
            request = self.youtube.videos().insert(
                part=','.join(body.keys()), body=body, media_body=media)

            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    logging.info(f"YouTube: {int(status.progress() * 100)}%")

            video_id = response['id']
            video_url = f"https://www.youtube.com/watch?v={video_id}"

            if thumbnail_path and os.path.exists(thumbnail_path):
                time.sleep(3)
                self._upload_thumbnail(video_id, thumbnail_path)

            if playlist_id:
                self._add_to_playlist(video_id, playlist_id)

            logging.info(f"✅ YouTube: {video_url}")
            return {'success': True, 'video_id': video_id, 'url': video_url}

        except Exception as e:
            logging.error(f"❌ YouTube: {e}")
            return {'success': False, 'error': str(e)}

    def _upload_thumbnail(self, video_id, thumbnail_path):
        try:
            self.youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(thumbnail_path)
            ).execute()
            logging.info("✅ YouTube thumbnail uploaded")
        except Exception as e:
            logging.error(f"❌ YouTube thumbnail: {e}")

    def _add_to_playlist(self, video_id, playlist_id):
        try:
            self.youtube.playlistItems().insert(
                part="snippet",
                body={'snippet': {
                    'playlistId': playlist_id,
                    'resourceId': {'kind': 'youtube#video', 'videoId': video_id}
                }}
            ).execute()
            logging.info("✅ YouTube: added to playlist")
        except Exception as e:
            logging.error(f"❌ YouTube playlist: {e}")
