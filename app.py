#!/usr/bin/env python3
"""Voitta Auth - macOS menu bar app for Microsoft OAuth2 authentication."""

import json
import os
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from dotenv import load_dotenv
import msal
import requests
import rumps

load_dotenv()

# Azure AD / Entra ID config
TENANT_ID = os.environ["AZURE_TENANT_ID"]
CLIENT_ID = os.environ["AZURE_CLIENT_ID"]
CLIENT_SECRET = os.environ["AZURE_CLIENT_SECRET"]
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPES = ["User.Read"]
REDIRECT_PORT = int(os.environ.get("REDIRECT_PORT", "53214"))
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}"


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Handles the OAuth2 redirect callback."""

    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        self.server.auth_code = query.get("code", [None])[0]
        self.server.auth_error = query.get("error_description", [None])[0]

        if self.server.auth_code:
            body = b"<html><body><h2>Authenticated! You can close this tab.</h2></body></html>"
        else:
            msg = self.server.auth_error or "Unknown error"
            body = f"<html><body><h2>Error: {msg}</h2></body></html>".encode()

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # silence logs


def _notify(title, subtitle, message):
    """Send a notification, falling back to alert if notifications aren't available."""
    try:
        rumps.notification(title, subtitle, message)
    except RuntimeError:
        rumps.alert(title=subtitle, message=message)


class VoittaAuthApp(rumps.App):
    def __init__(self):
        super().__init__("Voitta", title="V ○")
        self.token = None
        self.profile = None

        self.menu = [
            rumps.MenuItem("Authenticate", callback=self.authenticate),
            rumps.MenuItem("Deauthenticate", callback=self.deauthenticate),
            None,  # separator
            rumps.MenuItem("Help", callback=self.show_help),
        ]
        self._update_menu_state()

    def _update_menu_state(self):
        is_authed = self.token is not None
        self.title = "V ●" if is_authed else "V ○"
        self.menu["Authenticate"].set_callback(None if is_authed else self.authenticate)
        self.menu["Deauthenticate"].set_callback(self.deauthenticate if is_authed else None)

    def authenticate(self, _):
        """Start Microsoft OAuth2 device-code-free flow using local redirect."""
        threading.Thread(target=self._do_auth, daemon=True).start()

    def _do_auth(self):
        try:
            print("[voitta-auth] Starting OAuth2 flow...")
            app = msal.ConfidentialClientApplication(
                CLIENT_ID,
                authority=AUTHORITY,
                client_credential=CLIENT_SECRET,
            )

            auth_url = app.get_authorization_request_url(
                SCOPES, redirect_uri=REDIRECT_URI
            )
            print(f"[voitta-auth] Auth URL obtained, opening browser...")

            # Start a one-shot local HTTP server to catch the redirect
            server = HTTPServer(("127.0.0.1", REDIRECT_PORT), OAuthCallbackHandler)
            server.auth_code = None
            server.auth_error = None
            server.timeout = 120

            webbrowser.open(auth_url)
            print(f"[voitta-auth] Waiting for callback on port {REDIRECT_PORT}...")

            # Wait for the callback
            server.handle_request()
            server.server_close()

            if not server.auth_code:
                msg = server.auth_error or "No authorization code received."
                print(f"[voitta-auth] FAILED: {msg}")
                _notify("Voitta Auth", "Authentication failed", msg)
                return

            print("[voitta-auth] Got auth code, exchanging for token...")
            result = app.acquire_token_by_authorization_code(
                server.auth_code, scopes=SCOPES, redirect_uri=REDIRECT_URI
            )

            if "access_token" in result:
                self.token = result["access_token"]
                self._fetch_profile()
                name = self.profile.get("displayName", "Unknown") if self.profile else "Unknown"
                print(f"[voitta-auth] Authenticated as {name}")
                self._update_claude_json(add_auth=True)
                _notify("Voitta Auth", "Authenticated", f"Welcome, {name}!")
                self._update_menu_state()
            else:
                error = result.get("error_description", result.get("error", "Unknown error"))
                print(f"[voitta-auth] Token exchange failed: {error}")
                _notify("Voitta Auth", "Authentication failed", str(error))

        except Exception as e:
            print(f"[voitta-auth] EXCEPTION: {e}")
            import traceback
            traceback.print_exc()
            _notify("Voitta Auth", "Error", str(e))

    def _fetch_profile(self):
        """Fetch Microsoft Graph user profile."""
        try:
            resp = requests.get(
                "https://graph.microsoft.com/v1.0/me",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=10,
            )
            if resp.ok:
                self.profile = resp.json()
        except Exception:
            self.profile = None

    def _update_claude_json(self, add_auth=True):
        """Update ~/.claude.json mcpServers.voitta-rag headers."""
        claude_json_path = Path.home() / ".claude.json"
        if not claude_json_path.exists():
            print("[voitta-auth] ~/.claude.json not found")
            return

        with open(claude_json_path) as f:
            data = json.load(f)

        mcp_servers = data.get("mcpServers", {})
        voitta_rag = mcp_servers.get("voitta-rag")
        if voitta_rag is None:
            print("[voitta-auth] 'voitta-rag' not found in mcpServers")
            return

        headers = dict(voitta_rag.get("headers", {}))

        if add_auth:
            email = self.profile.get("mail") or self.profile.get("userPrincipalName", "") if self.profile else ""
            display_name = self.profile.get("displayName", "") if self.profile else ""
            headers.update({
                "X-Auth-Token": f"Bearer {self.token}",
                "X-Auth-Email": email,
                "X-Auth-Name": display_name,
            })
        else:
            for key in ("X-Auth-Token", "X-Auth-Email", "X-Auth-Name"):
                headers.pop(key, None)

        voitta_rag["headers"] = headers
        data["mcpServers"]["voitta-rag"] = voitta_rag

        with open(claude_json_path, "w") as f:
            json.dump(data, f, indent=2)

        action = "Added auth headers to" if add_auth else "Removed auth headers from"
        print(f"[voitta-auth] {action} ~/.claude.json")
        print(json.dumps({"voitta-rag": voitta_rag}, indent=2))

    def deauthenticate(self, _):
        """Clear token and profile."""
        self._update_claude_json(add_auth=False)
        self.token = None
        self.profile = None
        _notify("Voitta Auth", "Signed out", "Token cleared.")
        self._update_menu_state()

    def show_help(self, _):
        rumps.alert(
            title="Voitta Auth Help",
            message=(
                "Voitta Auth sits in your menu bar.\n\n"
                "• Authenticate — Sign in with your Microsoft account\n"
                "• Deauthenticate — Clear your session\n\n"
                "V ○ = signed out\n"
                "V ● = signed in"
            ),
        )


if __name__ == "__main__":
    VoittaAuthApp().run()
