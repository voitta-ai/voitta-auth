#!/usr/bin/env python3
"""Voitta Auth - macOS menu bar app for multi-provider OAuth2 authentication with MCP proxy."""

import base64
import hashlib
import json
import os
import secrets
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

from dotenv import load_dotenv
import msal
import requests
import rumps
from Foundation import NSObject, NSTimer, NSRunLoop

load_dotenv()

# ── Provider registry ────────────────────────────────────────────────────────

PROVIDERS = {
    "microsoft": {
        "label": "Microsoft",
        "settings_fields": [
            ("ms_tenant_id", "Tenant ID:"),
            ("ms_client_id", "Client ID:"),
        ],
        "env_defaults": {
            "ms_tenant_id": "AZURE_TENANT_ID",
            "ms_client_id": "AZURE_CLIENT_ID",
        },
    },
    "google": {
        "label": "Google",
        "settings_fields": [
            ("google_client_id", "Client ID:"),
            ("google_client_secret", "Client Secret:"),
        ],
        "env_defaults": {
            "google_client_id": "GOOGLE_CLIENT_ID",
            "google_client_secret": "GOOGLE_CLIENT_SECRET",
        },
    },
    "okta": {
        "label": "Okta",
        "settings_fields": [
            ("okta_domain", "Domain:"),
            ("okta_client_id", "Client ID:"),
        ],
        "env_defaults": {
            "okta_domain": "OKTA_DOMAIN",
            "okta_client_id": "OKTA_CLIENT_ID",
        },
    },
}

PROVIDER_ORDER = ("microsoft", "google", "okta")

# ── Global config ────────────────────────────────────────────────────────────

REDIRECT_PORT = int(os.environ.get("REDIRECT_PORT", "53214"))
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}"
PROXY_PORT = int(os.environ.get("PROXY_PORT", "18765"))
VOITTA_RAG_URL = os.environ.get("VOITTA_RAG_URL", "https://rag.voitta.ai")
SETTINGS_PATH = Path.home() / ".voitta_auth_settings.json"

# Hop-by-hop headers that must not be forwarded
HOP_BY_HOP = frozenset([
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
])


# ── Helpers ──────────────────────────────────────────────────────────────────

def _pkce_pair():
    """Generate PKCE code_verifier and code_challenge (S256)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def _notify(title, subtitle, message):
    try:
        rumps.notification(title, subtitle, message)
    except Exception:
        pass


class _FocusTrigger(NSObject):
    """Grabs keyboard focus for an NSAlert on the main thread after a short delay."""

    def setWindow_field_(self, win, field):
        self._win = win
        self._field = field

    def focus_(self, _):
        from AppKit import NSApp
        NSApp.activateIgnoringOtherApps_(True)
        self._win.makeKeyAndOrderFront_(None)
        if self._field is not None:
            self._win.makeFirstResponder_(self._field)


def _show_modal(alert, first_field=None):
    """Run an NSAlert as a floating modal with proper keyboard focus."""
    from AppKit import NSApp, NSFloatingWindowLevel

    alert_window = alert.window()
    alert_window.setLevel_(NSFloatingWindowLevel)
    alert.layout()
    if first_field:
        alert_window.setInitialFirstResponder_(first_field)

    NSApp.setActivationPolicy_(0)
    NSApp.activateIgnoringOtherApps_(True)

    trigger = _FocusTrigger.alloc().init()
    trigger.setWindow_field_(alert_window, first_field)
    timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
        0.1, trigger, "focus:", None, False
    )
    NSRunLoop.mainRunLoop().addTimer_forMode_(timer, "NSDefaultRunLoopMode")
    NSRunLoop.mainRunLoop().addTimer_forMode_(timer, "NSModalPanelRunLoopMode")

    result = alert.runModal()
    NSApp.setActivationPolicy_(1)
    return result


# ── HTTP handlers ────────────────────────────────────────────────────────────

class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Handles the OAuth2 redirect callback for all providers."""

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
    """HTTP pass-through proxy that injects auth headers for all authenticated providers."""

    voitta_app = None

    def _proxy(self):
        app = self.__class__.voitta_app
        base_url = app.voitta_rag_url if app else VOITTA_RAG_URL
        target_url = base_url.rstrip("/") + self.path
        print(f"[proxy] {self.command} {self.path} → {target_url}")

        headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in HOP_BY_HOP and k.lower() != "host"
        }

        # Inject auth headers for all authenticated providers
        any_injected = False
        if app:
            for key in PROVIDER_ORDER:
                state = app._auth[key]
                if not state["token"]:
                    continue
                suffix = PROVIDERS[key]["label"]
                headers[f"X-Auth-Token-{suffix}"] = f"Bearer {state['token']}"
                if state["profile"]:
                    headers[f"X-Auth-Email-{suffix}"] = state["profile"].get("email", "")
                    headers[f"X-Auth-Name-{suffix}"] = state["profile"].get("name", "")
                any_injected = True
                print(f"[proxy] {key} auth injected")

        if not any_injected:
            print("[proxy] WARNING: no tokens available, forwarding unauthenticated")

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


# ── Main application ─────────────────────────────────────────────────────────

class VoittaAuthApp(rumps.App):
    def __init__(self):
        super().__init__("Voitta", title="V ○ ○ ○")

        self._auth_lock = threading.Lock()
        self._auth = {}
        for key in PROVIDER_ORDER:
            self._auth[key] = {
                "token": None,
                "refresh_token": None,
                "profile": None,
                "refresh_timer": None,
                "msal_app": None,
            }

        ProxyHandler.voitta_app = self

        # Load persistent settings
        self._settings = self._load_settings()
        self.voitta_rag_url = self._settings.get("voitta_rag_url", VOITTA_RAG_URL)

        # Load per-provider credentials from settings (fall back to env vars)
        for key, cfg in PROVIDERS.items():
            for settings_key, env_var in cfg["env_defaults"].items():
                if settings_key not in self._settings:
                    self._settings[settings_key] = os.environ.get(env_var, "")

        # Initialize MSAL for Microsoft
        self._rebuild_msal_app()

        # Build menu with direct references
        self._menu_items = {}
        menu_list = []
        for key in PROVIDER_ORDER:
            label = PROVIDERS[key]["label"]
            item = rumps.MenuItem(f"Activate {label}", callback=self._make_toggle_callback(key))
            self._menu_items[key] = item
            menu_list.append(item)
        menu_list.append(None)  # separator
        menu_list.append(rumps.MenuItem("Settings", callback=self.show_settings))
        menu_list.append(rumps.MenuItem("Help", callback=self.show_help))
        self.menu = menu_list

        self._update_menu_state()

        # Start MCP proxy server
        threading.Thread(target=self._run_proxy, daemon=True).start()

    # ── Menu state ───────────────────────────────────────────────────────────

    def _build_title(self):
        indicators = []
        for key in PROVIDER_ORDER:
            indicators.append("●" if self._auth[key]["token"] else "○")
        return "V " + " ".join(indicators)

    def _update_menu_state(self):
        self.title = self._build_title()
        for key in PROVIDER_ORDER:
            label = PROVIDERS[key]["label"]
            item = self._menu_items[key]
            if self._auth[key]["token"]:
                item.title = f"Deactivate {label}"
            else:
                item.title = f"Activate {label}"

    def _make_toggle_callback(self, provider_key):
        def callback(_):
            if self._auth[provider_key]["token"]:
                self._deauth_provider(provider_key)
            else:
                threading.Thread(
                    target=self._do_auth, args=(provider_key,), daemon=True
                ).start()
        return callback

    # ── MSAL (Microsoft) ─────────────────────────────────────────────────────

    def _rebuild_msal_app(self):
        tenant_id = self._settings.get("ms_tenant_id", "")
        client_id = self._settings.get("ms_client_id", "")
        if tenant_id and client_id:
            self._auth["microsoft"]["msal_app"] = msal.PublicClientApplication(
                client_id,
                authority=f"https://login.microsoftonline.com/{tenant_id}",
            )
        else:
            self._auth["microsoft"]["msal_app"] = None

    # ── Auth dispatcher ──────────────────────────────────────────────────────

    def _do_auth(self, provider_key):
        if not self._auth_lock.acquire(blocking=False):
            _notify("Voitta Auth", "Busy", "Another authentication is in progress.")
            return
        try:
            label = PROVIDERS[provider_key]["label"]
            print(f"[voitta-auth] Starting {label} OAuth2 flow...")
            if provider_key == "microsoft":
                self._do_auth_microsoft()
            elif provider_key == "google":
                self._do_auth_google()
            elif provider_key == "okta":
                self._do_auth_okta()
        except Exception as e:
            print(f"[voitta-auth] {provider_key} EXCEPTION: {e}")
            traceback.print_exc()
            _notify("Voitta Auth", "Error", str(e))
        finally:
            self._auth_lock.release()

    def _wait_for_callback(self):
        """Start a one-shot HTTP server, wait for the OAuth callback, return (code, error)."""
        server = HTTPServer(("127.0.0.1", REDIRECT_PORT), OAuthCallbackHandler)
        server.auth_code = None
        server.auth_error = None
        server.timeout = 120
        print(f"[voitta-auth] Waiting for callback on port {REDIRECT_PORT}...")
        server.handle_request()
        server.server_close()
        return server.auth_code, server.auth_error

    # ── Microsoft auth ───────────────────────────────────────────────────────

    def _do_auth_microsoft(self):
        state = self._auth["microsoft"]
        msal_app = state["msal_app"]
        if not msal_app:
            _notify("Voitta Auth", "Microsoft", "Configure Tenant ID and Client ID in Settings first.")
            return

        auth_url = msal_app.get_authorization_request_url(
            ["User.Read"], redirect_uri=REDIRECT_URI
        )
        webbrowser.open(auth_url)
        code, error = self._wait_for_callback()

        if not code:
            _notify("Voitta Auth", "Microsoft", error or "No authorization code received.")
            return

        print("[voitta-auth] Got auth code, exchanging for token...")
        result = msal_app.acquire_token_by_authorization_code(
            code, scopes=["User.Read"], redirect_uri=REDIRECT_URI
        )

        if "access_token" in result:
            state["token"] = result["access_token"]
            self._fetch_profile_microsoft()
            self._schedule_refresh("microsoft", result.get("expires_in", 3600))
            name = state["profile"].get("name", "Unknown") if state["profile"] else "Unknown"
            print(f"[voitta-auth] Microsoft authenticated as {name}")
            self._update_menu_state()
            _notify("Voitta Auth", "Microsoft", f"Welcome, {name}!")
        else:
            error = result.get("error_description", result.get("error", "Unknown error"))
            print(f"[voitta-auth] Microsoft token exchange failed: {error}")
            _notify("Voitta Auth", "Microsoft", str(error))

    def _fetch_profile_microsoft(self):
        state = self._auth["microsoft"]
        try:
            resp = requests.get(
                "https://graph.microsoft.com/v1.0/me",
                headers={"Authorization": f"Bearer {state['token']}"},
                timeout=10,
            )
            if resp.ok:
                data = resp.json()
                state["profile"] = {
                    "email": data.get("mail") or data.get("userPrincipalName", ""),
                    "name": data.get("displayName", ""),
                }
        except Exception:
            state["profile"] = None

    # ── Google auth ──────────────────────────────────────────────────────────

    def _do_auth_google(self):
        client_id = self._settings.get("google_client_id", "")
        client_secret = self._settings.get("google_client_secret", "")
        if not client_id or not client_secret:
            msg = "Configure Client ID and Client Secret in Settings first."
            print(f"[voitta-auth] Google: {msg} (client_id={'set' if client_id else 'MISSING'}, client_secret={'set' if client_secret else 'MISSING'})")
            _notify("Voitta Auth", "Google", msg)
            return

        verifier, challenge = _pkce_pair()
        params = {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": "openid email profile",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
        }
        auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
        webbrowser.open(auth_url)
        code, error = self._wait_for_callback()

        if not code:
            _notify("Voitta Auth", "Google", error or "No authorization code received.")
            return

        print("[voitta-auth] Got auth code, exchanging for token...")
        resp = requests.post("https://oauth2.googleapis.com/token", data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": REDIRECT_URI,
        }, timeout=10)

        if not resp.ok:
            _notify("Voitta Auth", "Google", f"Token exchange failed: {resp.text[:200]}")
            return

        data = resp.json()
        state = self._auth["google"]
        state["token"] = data["access_token"]
        state["refresh_token"] = data.get("refresh_token")
        self._fetch_profile_google()
        self._schedule_refresh("google", data.get("expires_in", 3600))
        name = state["profile"].get("name", "Unknown") if state["profile"] else "Unknown"
        print(f"[voitta-auth] Google authenticated as {name}")
        self._update_menu_state()
        _notify("Voitta Auth", "Google", f"Welcome, {name}!")

    def _fetch_profile_google(self):
        state = self._auth["google"]
        try:
            resp = requests.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {state['token']}"},
                timeout=10,
            )
            if resp.ok:
                data = resp.json()
                state["profile"] = {
                    "email": data.get("email", ""),
                    "name": data.get("name", ""),
                }
        except Exception:
            state["profile"] = None

    # ── Okta auth ────────────────────────────────────────────────────────────

    def _do_auth_okta(self):
        domain = self._settings.get("okta_domain", "")
        client_id = self._settings.get("okta_client_id", "")
        if not domain or not client_id:
            _notify("Voitta Auth", "Okta", "Configure Domain and Client ID in Settings first.")
            return

        verifier, challenge = _pkce_pair()
        params = {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": "openid email profile offline_access",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        auth_url = f"https://{domain}/oauth2/default/v1/authorize?" + urlencode(params)
        webbrowser.open(auth_url)
        code, error = self._wait_for_callback()

        if not code:
            _notify("Voitta Auth", "Okta", error or "No authorization code received.")
            return

        print("[voitta-auth] Got auth code, exchanging for token...")
        resp = requests.post(f"https://{domain}/oauth2/default/v1/token", data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": REDIRECT_URI,
        }, timeout=10)

        if not resp.ok:
            _notify("Voitta Auth", "Okta", f"Token exchange failed: {resp.text[:200]}")
            return

        data = resp.json()
        state = self._auth["okta"]
        state["token"] = data["access_token"]
        state["refresh_token"] = data.get("refresh_token")
        self._fetch_profile_okta()
        self._schedule_refresh("okta", data.get("expires_in", 3600))
        name = state["profile"].get("name", "Unknown") if state["profile"] else "Unknown"
        print(f"[voitta-auth] Okta authenticated as {name}")
        self._update_menu_state()
        _notify("Voitta Auth", "Okta", f"Welcome, {name}!")

    def _fetch_profile_okta(self):
        state = self._auth["okta"]
        domain = self._settings.get("okta_domain", "")
        try:
            resp = requests.get(
                f"https://{domain}/oauth2/default/v1/userinfo",
                headers={"Authorization": f"Bearer {state['token']}"},
                timeout=10,
            )
            if resp.ok:
                data = resp.json()
                state["profile"] = {
                    "email": data.get("email", ""),
                    "name": data.get("name", ""),
                }
        except Exception:
            state["profile"] = None

    # ── Token refresh ────────────────────────────────────────────────────────

    def _schedule_refresh(self, provider_key, expires_in):
        state = self._auth[provider_key]
        if state["refresh_timer"]:
            state["refresh_timer"].cancel()
        refresh_in = max(expires_in - 300, 60)

        if provider_key == "microsoft":
            timer = threading.Timer(refresh_in, self._do_refresh_microsoft)
        elif provider_key == "google":
            timer = threading.Timer(refresh_in, self._do_refresh_generic,
                                    args=("google", "https://oauth2.googleapis.com/token"))
        elif provider_key == "okta":
            domain = self._settings.get("okta_domain", "")
            timer = threading.Timer(refresh_in, self._do_refresh_generic,
                                    args=("okta", f"https://{domain}/oauth2/default/v1/token"))

        timer.daemon = True
        timer.start()
        state["refresh_timer"] = timer
        print(f"[voitta-auth] {provider_key} token refresh scheduled in {refresh_in}s")

    def _do_refresh_microsoft(self):
        state = self._auth["microsoft"]
        msal_app = state["msal_app"]
        if not msal_app:
            return
        accounts = msal_app.get_accounts()
        if not accounts:
            return
        result = msal_app.acquire_token_silent(["User.Read"], account=accounts[0], force_refresh=True)
        if result and "access_token" in result:
            state["token"] = result["access_token"]
            self._schedule_refresh("microsoft", result.get("expires_in", 3600))
            print("[voitta-auth] Microsoft token refreshed silently")
        else:
            print("[voitta-auth] Microsoft silent refresh failed — user must re-authenticate")
            state["token"] = None
            state["profile"] = None
            self._update_menu_state()

    def _do_refresh_generic(self, provider_key, token_endpoint):
        state = self._auth[provider_key]
        if not state["refresh_token"]:
            return

        # Determine credentials based on provider
        if provider_key == "google":
            client_id = self._settings.get("google_client_id", "")
            client_secret = self._settings.get("google_client_secret", "")
        elif provider_key == "okta":
            client_id = self._settings.get("okta_client_id", "")
            client_secret = None
        else:
            return

        try:
            post_data = {
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": state["refresh_token"],
            }
            if client_secret:
                post_data["client_secret"] = client_secret
            resp = requests.post(token_endpoint, data=post_data, timeout=10)

            if resp.ok:
                data = resp.json()
                state["token"] = data["access_token"]
                if "refresh_token" in data:
                    state["refresh_token"] = data["refresh_token"]
                self._schedule_refresh(provider_key, data.get("expires_in", 3600))
                print(f"[voitta-auth] {provider_key} token refreshed silently")
            else:
                print(f"[voitta-auth] {provider_key} silent refresh failed — user must re-authenticate")
                state["token"] = None
                state["refresh_token"] = None
                state["profile"] = None
                self._update_menu_state()
        except Exception as e:
            print(f"[voitta-auth] {provider_key} refresh error: {e}")
            state["token"] = None
            state["refresh_token"] = None
            state["profile"] = None
            self._update_menu_state()

    # ── Deauthentication ─────────────────────────────────────────────────────

    def _deauth_provider(self, provider_key):
        state = self._auth[provider_key]
        if state["refresh_timer"]:
            state["refresh_timer"].cancel()
            state["refresh_timer"] = None

        if provider_key == "microsoft" and state["msal_app"]:
            for account in state["msal_app"].get_accounts():
                state["msal_app"].remove_account(account)

        state["token"] = None
        state["refresh_token"] = None
        state["profile"] = None

        label = PROVIDERS[provider_key]["label"]
        print(f"[voitta-auth] {label} signed out")
        _notify("Voitta Auth", label, "Signed out.")
        self._update_menu_state()

    # ── Proxy ────────────────────────────────────────────────────────────────

    def _run_proxy(self):
        server = ThreadingHTTPServer(("127.0.0.1", PROXY_PORT), ProxyHandler)
        print(f"[voitta-auth] Proxy listening on http://127.0.0.1:{PROXY_PORT} → {self.voitta_rag_url}")
        server.serve_forever()

    # ── Settings ─────────────────────────────────────────────────────────────

    def _load_settings(self):
        if SETTINGS_PATH.exists():
            try:
                with open(SETTINGS_PATH) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_settings(self):
        data = {"voitta_rag_url": self.voitta_rag_url}
        for cfg in PROVIDERS.values():
            for settings_key, _ in cfg["settings_fields"]:
                data[settings_key] = self._settings.get(settings_key, "")
        with open(SETTINGS_PATH, "w") as f:
            json.dump(data, f, indent=2)
        print("[voitta-auth] Settings saved")

    def show_settings(self, _):
        from AppKit import (
            NSApp, NSAlert, NSFont,
            NSTextField, NSView,
        )
        from Foundation import NSMakeRect

        label_w, field_w, row_h, header_h, gap = 120, 310, 22, 20, 8
        total_w = label_w + field_w

        # Build row descriptors: (label, value_or_None, settings_key_or_None, is_header)
        rows = [("Voitta RAG URL:", self.voitta_rag_url, "voitta_rag_url", False)]
        for key in PROVIDER_ORDER:
            cfg = PROVIDERS[key]
            rows.append((f"── {cfg['label']} ──", None, None, True))
            for settings_key, field_label in cfg["settings_fields"]:
                rows.append((field_label, self._settings.get(settings_key, ""), settings_key, False))

        # Calculate total height
        total_h = 0
        for i, (_, _, _, is_header) in enumerate(rows):
            total_h += header_h if is_header else row_h
            if i > 0:
                total_h += gap

        container = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, total_w, total_h))
        text_fields = []       # (settings_key, NSTextField)
        bold_font = NSFont.boldSystemFontOfSize_(11)

        y_cursor = total_h
        for label_text, value, settings_key, is_header in rows:
            h = header_h if is_header else row_h
            y_cursor -= h

            if is_header:
                lbl = NSTextField.alloc().initWithFrame_(
                    NSMakeRect(0, y_cursor, total_w, h)
                )
                lbl.setStringValue_(label_text)
                lbl.setBezeled_(False)
                lbl.setDrawsBackground_(False)
                lbl.setEditable_(False)
                lbl.setSelectable_(False)
                lbl.setFont_(bold_font)
                lbl.setAlignment_(0)   # NSTextAlignmentLeft
                container.addSubview_(lbl)
            else:
                lbl = NSTextField.alloc().initWithFrame_(
                    NSMakeRect(0, y_cursor + 1, label_w - 6, row_h)
                )
                lbl.setStringValue_(label_text)
                lbl.setBezeled_(False)
                lbl.setDrawsBackground_(False)
                lbl.setEditable_(False)
                lbl.setSelectable_(False)
                lbl.setAlignment_(1)   # NSTextAlignmentRight
                container.addSubview_(lbl)

                fld = NSTextField.alloc().initWithFrame_(
                    NSMakeRect(label_w, y_cursor, field_w, row_h)
                )
                fld.setStringValue_(value or "")
                container.addSubview_(fld)
                text_fields.append((settings_key, fld))

            y_cursor -= gap

        alert = NSAlert.alloc().init()
        alert.setMessageText_("Voitta Auth Settings")
        alert.setInformativeText_("Saved to ~/.voitta_auth_settings.json")
        alert.addButtonWithTitle_("Save")
        alert.addButtonWithTitle_("Cancel")
        alert.setAccessoryView_(container)

        first_fld = text_fields[0][1] if text_fields else None
        result = _show_modal(alert, first_field=first_fld)

        if result != 1000:
            return

        # Collect new values
        old_settings = dict(self._settings)
        self.voitta_rag_url = text_fields[0][1].stringValue().strip().rstrip("/") or self.voitta_rag_url

        for settings_key, fld in text_fields:
            val = fld.stringValue().strip()
            if settings_key == "voitta_rag_url":
                continue  # already handled
            self._settings[settings_key] = val

        self._save_settings()

        # Detect per-provider credential changes and clear affected sessions
        for key, cfg in PROVIDERS.items():
            changed = False
            for settings_key, _ in cfg["settings_fields"]:
                if self._settings.get(settings_key, "") != old_settings.get(settings_key, ""):
                    changed = True
                    break
            if changed:
                self._deauth_provider(key)
                if key == "microsoft":
                    self._rebuild_msal_app()
                print(f"[voitta-auth] {PROVIDERS[key]['label']} credentials changed — session cleared")

    # ── Help ─────────────────────────────────────────────────────────────────

    def show_help(self, _):
        from AppKit import NSAlert
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Voitta Auth Help")
        alert.setInformativeText_(
            "Voitta Auth sits in your menu bar.\n\n"
            "Status: V [M] [G] [O]\n"
            "  ● = authenticated  ○ = not authenticated\n\n"
            "Activate/Deactivate each provider independently.\n"
            "All authenticated providers inject headers into the proxy.\n\n"
            f"Proxy: http://127.0.0.1:{PROXY_PORT} → {self.voitta_rag_url}"
        )
        alert.addButtonWithTitle_("OK")
        _show_modal(alert)


if __name__ == "__main__":
    VoittaAuthApp().run()
