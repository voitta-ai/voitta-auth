#!/usr/bin/env python3
"""Voitta Auth - macOS menu bar app for Microsoft OAuth2 authentication with MCP proxy."""

import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from dotenv import load_dotenv
import msal
import requests
import rumps
from Foundation import NSObject, NSTimer, NSRunLoop

load_dotenv()

# Azure AD / Entra ID config — env vars are bootstrap defaults; runtime values
# are loaded from the settings file and take precedence.
TENANT_ID = os.environ.get("AZURE_TENANT_ID", "")
CLIENT_ID = os.environ.get("AZURE_CLIENT_ID", "")
SCOPES = ["User.Read"]
REDIRECT_PORT = int(os.environ.get("REDIRECT_PORT", "53214"))
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}"

# Proxy config
PROXY_PORT = int(os.environ.get("PROXY_PORT", "18765"))
VOITTA_RAG_URL = os.environ.get("VOITTA_RAG_URL", "https://rag.voitta.ai")

# Settings file
SETTINGS_PATH = Path.home() / ".voitta_auth_settings.json"

class _FocusTrigger(NSObject):
    """Grabs keyboard focus for an NSAlert on the main thread after a short delay.
    Required for LSUIElement apps where runModal resets activation."""

    def setWindow_field_(self, win, field):
        self._win = win
        self._field = field

    def focus_(self, _):
        from AppKit import NSApp
        NSApp.activateIgnoringOtherApps_(True)
        self._win.makeKeyAndOrderFront_(None)
        if self._field is not None:
            self._win.makeFirstResponder_(self._field)


# Hop-by-hop headers that must not be forwarded
HOP_BY_HOP = frozenset([
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
])


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
        pass


class ProxyHandler(BaseHTTPRequestHandler):
    """HTTP pass-through proxy that injects auth headers into requests to voitta-rag."""

    voitta_app = None  # set by VoittaAuthApp.__init__

    def _proxy(self):
        app = self.__class__.voitta_app
        base_url = app.voitta_rag_url if app else VOITTA_RAG_URL
        target_url = base_url.rstrip("/") + self.path
        print(f"[proxy] {self.command} {self.path} → {target_url}")

        # Forward all headers except hop-by-hop and Host
        headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in HOP_BY_HOP and k.lower() != "host"
        }

        # Inject auth headers
        if app and app.token:
            headers["X-Auth-Token"] = f"Bearer {app.token}"
            if app.profile:
                headers["X-Auth-Email"] = (
                    app.profile.get("mail") or app.profile.get("userPrincipalName", "")
                )
                headers["X-Auth-Name"] = app.profile.get("displayName", "")
            print(f"[proxy] auth injected for {headers.get('X-Auth-Email', '?')}")
        else:
            print(f"[proxy] WARNING: no token available, forwarding unauthenticated")

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else None
        if body:
            print(f"[proxy] body ({content_length}b): {body[:200]}")

        try:
            resp = requests.request(
                self.command, target_url,
                headers=headers, data=body,
                stream=True, timeout=(10, None),
            )
        except Exception as e:
            print(f"[proxy] ERROR connecting to upstream: {e}")
            self.send_error(502, f"Proxy error: {e}")
            return

        print(f"[proxy] upstream response: {resp.status_code} {resp.reason}")
        if resp.status_code >= 400:
            print(f"[proxy] upstream headers: {dict(resp.headers)}")
            body_preview = resp.content[:500]
            print(f"[proxy] upstream error body: {body_preview}")
            self.send_response(resp.status_code)
            for k, v in resp.headers.items():
                if k.lower() not in HOP_BY_HOP:
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body_preview)
            return

        self.send_response(resp.status_code)
        for k, v in resp.headers.items():
            if k.lower() not in HOP_BY_HOP:
                self.send_header(k, v)
        self.end_headers()

        try:
            for chunk in resp.iter_content(chunk_size=None):
                if chunk:
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self): self._proxy()
    def do_POST(self): self._proxy()
    def do_PUT(self): self._proxy()
    def do_DELETE(self): self._proxy()
    def do_PATCH(self): self._proxy()
    def do_HEAD(self): self._proxy()
    def do_OPTIONS(self): self._proxy()

    def log_message(self, format, *args):
        pass


def _notify(title, subtitle, message):
    try:
        rumps.notification(title, subtitle, message)
    except Exception:
        pass  # notifications require Info.plist; ignore silently, output already printed


class VoittaAuthApp(rumps.App):
    def __init__(self):
        super().__init__("Voitta", title="V ○")
        self.token = None
        self.profile = None
        self._refresh_timer = None

        ProxyHandler.voitta_app = self

        # Load persistent settings (overrides .env defaults)
        self._settings = self._load_settings()
        self.voitta_rag_url = self._settings.get("voitta_rag_url", VOITTA_RAG_URL)

        # Azure credentials: settings file overrides env-var defaults.
        # Both are public identifiers — safe to store in the JSON settings file.
        self._tenant_id = self._settings.get("tenant_id") or TENANT_ID
        self._client_id = self._settings.get("client_id") or CLIENT_ID

        self._msal_app = msal.PublicClientApplication(
            self._client_id,
            authority=f"https://login.microsoftonline.com/{self._tenant_id}",
        )

        self.menu = [
            rumps.MenuItem("Authenticate", callback=self.authenticate),
            rumps.MenuItem("Deauthenticate", callback=self.deauthenticate),
            None,
            rumps.MenuItem("Settings", callback=self.show_settings),
            rumps.MenuItem("Help", callback=self.show_help),
        ]
        self._update_menu_state()

        # Start MCP proxy server
        threading.Thread(target=self._run_proxy, daemon=True).start()

    def _rebuild_msal_app(self):
        """Recreate the MSAL app after credential changes."""
        self._msal_app = msal.PublicClientApplication(
            self._client_id,
            authority=f"https://login.microsoftonline.com/{self._tenant_id}",
        )

    def _schedule_refresh(self, expires_in):
        """Schedule a proactive token refresh 5 minutes before expiry."""
        if self._refresh_timer:
            self._refresh_timer.cancel()
        refresh_in = max(expires_in - 300, 60)
        self._refresh_timer = threading.Timer(refresh_in, self._do_silent_refresh)
        self._refresh_timer.daemon = True
        self._refresh_timer.start()
        print(f"[voitta-auth] Token refresh scheduled in {refresh_in}s")

    def _do_silent_refresh(self):
        """Proactively refresh the access token using the cached refresh token."""
        accounts = self._msal_app.get_accounts()
        if not accounts:
            return
        result = self._msal_app.acquire_token_silent(SCOPES, account=accounts[0], force_refresh=True)
        if result and "access_token" in result:
            self.token = result["access_token"]
            self._schedule_refresh(result.get("expires_in", 3600))
            print("[voitta-auth] Token refreshed silently")
        else:
            print("[voitta-auth] Silent refresh failed — user must re-authenticate")
            self.token = None
            self._update_menu_state()

    def _run_proxy(self):
        server = ThreadingHTTPServer(("127.0.0.1", PROXY_PORT), ProxyHandler)
        print(f"[voitta-auth] Proxy listening on http://127.0.0.1:{PROXY_PORT} → {self.voitta_rag_url}")
        server.serve_forever()

    def _update_menu_state(self):
        is_authed = self.token is not None
        self.title = "V ●" if is_authed else "V ○"
        self.menu["Authenticate"].set_callback(None if is_authed else self.authenticate)
        self.menu["Deauthenticate"].set_callback(self.deauthenticate if is_authed else None)

    def authenticate(self, _):
        threading.Thread(target=self._do_auth, daemon=True).start()

    def _do_auth(self):
        try:
            print("[voitta-auth] Starting OAuth2 flow...")
            auth_url = self._msal_app.get_authorization_request_url(
                SCOPES, redirect_uri=REDIRECT_URI
            )

            server = HTTPServer(("127.0.0.1", REDIRECT_PORT), OAuthCallbackHandler)
            server.auth_code = None
            server.auth_error = None
            server.timeout = 120

            webbrowser.open(auth_url)
            print(f"[voitta-auth] Waiting for callback on port {REDIRECT_PORT}...")
            server.handle_request()
            server.server_close()

            if not server.auth_code:
                msg = server.auth_error or "No authorization code received."
                print(f"[voitta-auth] FAILED: {msg}")
                _notify("Voitta Auth", "Authentication failed", msg)
                return

            print("[voitta-auth] Got auth code, exchanging for token...")
            result = self._msal_app.acquire_token_by_authorization_code(
                server.auth_code, scopes=SCOPES, redirect_uri=REDIRECT_URI
            )

            if "access_token" in result:
                self.token = result["access_token"]
                self._fetch_profile()
                self._schedule_refresh(result.get("expires_in", 3600))
                name = self.profile.get("displayName", "Unknown") if self.profile else "Unknown"
                print(f"[voitta-auth] Authenticated as {name}")
                self._update_menu_state()
                _notify("Voitta Auth", "Authenticated", f"Welcome, {name}!")
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

    def deauthenticate(self, _):
        if self._refresh_timer:
            self._refresh_timer.cancel()
            self._refresh_timer = None
        for account in self._msal_app.get_accounts():
            self._msal_app.remove_account(account)
        self.token = None
        self.profile = None
        _notify("Voitta Auth", "Signed out", "Token cleared.")
        self._update_menu_state()

    def _load_settings(self):
        if SETTINGS_PATH.exists():
            try:
                with open(SETTINGS_PATH) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_settings(self):
        data = {
            "voitta_rag_url": self.voitta_rag_url,
            "tenant_id": self._tenant_id,
            "client_id": self._client_id,
        }
        with open(SETTINGS_PATH, "w") as f:
            json.dump(data, f, indent=2)
        print(f"[voitta-auth] Settings saved")

    def show_settings(self, _):
        from AppKit import (
            NSApp, NSAlert, NSFloatingWindowLevel,
            NSTextField, NSView,
        )
        from Foundation import NSMakeRect

        # ── Layout ────────────────────────────────────────────────────────────
        label_w, field_w, row_h, gap = 120, 310, 22, 10
        rows = [
            ("Voitta RAG URL:",  self.voitta_rag_url),
            ("Tenant ID:",       self._tenant_id),
            ("Client ID:",       self._client_id),
        ]
        total_w = label_w + field_w
        total_h = len(rows) * row_h + (len(rows) - 1) * gap

        container = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, total_w, total_h))
        text_fields = []

        for i, (label_text, value) in enumerate(rows):
            # NSView uses non-flipped coords (y=0 at bottom); top row has highest y.
            y = total_h - (i + 1) * row_h - i * gap

            lbl = NSTextField.alloc().initWithFrame_(
                NSMakeRect(0, y + 1, label_w - 6, row_h)
            )
            lbl.setStringValue_(label_text)
            lbl.setBezeled_(False)
            lbl.setDrawsBackground_(False)
            lbl.setEditable_(False)
            lbl.setSelectable_(False)
            lbl.setAlignment_(1)   # NSTextAlignmentRight
            container.addSubview_(lbl)

            fld = NSTextField.alloc().initWithFrame_(
                NSMakeRect(label_w, y, field_w, row_h)
            )
            fld.setStringValue_(value or "")
            container.addSubview_(fld)
            text_fields.append(fld)

        # ── Alert ─────────────────────────────────────────────────────────────
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Voitta Auth Settings")
        alert.setInformativeText_(
            "All values are saved to ~/.voitta_auth_settings.json."
        )
        alert.addButtonWithTitle_("Save")
        alert.addButtonWithTitle_("Cancel")
        alert.setAccessoryView_(container)

        alert_window = alert.window()
        alert_window.setLevel_(NSFloatingWindowLevel)

        # Force layout so the text fields are in the view hierarchy, then hint
        # which field receives initial keyboard focus.
        alert.layout()
        alert_window.setInitialFirstResponder_(text_fields[0])

        # Briefly switch to Regular activation policy so macOS routes keyboard
        # events to us (LSUIElement apps don't get keyboard focus otherwise).
        NSApp.setActivationPolicy_(0)   # NSApplicationActivationPolicyRegular
        NSApp.activateIgnoringOtherApps_(True)

        # Safety-net timer re-asserts focus once the modal run-loop is spinning.
        trigger = _FocusTrigger.alloc().init()
        trigger.setWindow_field_(alert_window, text_fields[0])
        timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            0.1, trigger, "focus:", None, False
        )
        NSRunLoop.mainRunLoop().addTimer_forMode_(timer, "NSDefaultRunLoopMode")
        NSRunLoop.mainRunLoop().addTimer_forMode_(timer, "NSModalPanelRunLoopMode")

        result = alert.runModal()   # NSAlertFirstButtonReturn == 1000

        # Restore menu-bar-only policy (no dock icon).
        NSApp.setActivationPolicy_(1)   # NSApplicationActivationPolicyAccessory

        if result != 1000:   # Cancel
            return

        new_rag_url   = text_fields[0].stringValue().strip().rstrip("/")
        new_tenant_id = text_fields[1].stringValue().strip()
        new_client_id = text_fields[2].stringValue().strip()

        creds_changed = (
            new_tenant_id != self._tenant_id
            or new_client_id != self._client_id
        )

        self.voitta_rag_url = new_rag_url   or self.voitta_rag_url
        self._tenant_id     = new_tenant_id or self._tenant_id
        self._client_id     = new_client_id or self._client_id
        self._save_settings()

        if creds_changed:
            # Clear the cached session — tokens belong to the old credentials.
            if self._refresh_timer:
                self._refresh_timer.cancel()
                self._refresh_timer = None
            for account in self._msal_app.get_accounts():
                self._msal_app.remove_account(account)
            self.token = None
            self.profile = None
            self._rebuild_msal_app()
            self._update_menu_state()
            print("[voitta-auth] Credentials changed — MSAL app rebuilt, session cleared")

    def show_help(self, _):
        from AppKit import NSAlert, NSFloatingWindowLevel
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Voitta Auth Help")
        alert.setInformativeText_(
            "Voitta Auth sits in your menu bar.\n\n"
            "• Authenticate — Sign in with your Microsoft account\n"
            "• Deauthenticate — Clear your session\n\n"
            "V ○ = signed out\n"
            "V ● = signed in\n\n"
            f"Proxy: http://127.0.0.1:{PROXY_PORT} → {self.voitta_rag_url}"
        )
        alert.addButtonWithTitle_("OK")
        alert.window().setLevel_(NSFloatingWindowLevel)
        trigger = _FocusTrigger.alloc().init()
        trigger.setWindow_field_(alert.window(), None)
        timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            0.1, trigger, "focus:", None, False
        )
        NSRunLoop.mainRunLoop().addTimer_forMode_(timer, "NSDefaultRunLoopMode")
        NSRunLoop.mainRunLoop().addTimer_forMode_(timer, "NSModalPanelRunLoopMode")
        alert.runModal()


if __name__ == "__main__":
    VoittaAuthApp().run()
