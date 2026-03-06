#!/usr/bin/env python3
"""Voitta Auth - macOS menu bar app for multi-provider OAuth2 authentication with MCP proxy."""

import atexit
import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

import logging

from dotenv import load_dotenv
import msal
import requests
import rumps
from fastmcp import FastMCP as FastMCPServer
from fastmcp.server.providers.proxy import FastMCPProxy, ProxyClient
from fastmcp.client.transports import StreamableHttpTransport

# Surface FastMCP proxy errors (otherwise silently swallowed at DEBUG)
logging.getLogger("fastmcp.server.providers.aggregate").setLevel(logging.DEBUG)
logging.basicConfig(level=logging.WARNING, format="[%(name)s] %(levelname)s: %(message)s")
logging.getLogger("fastmcp.server.providers.aggregate").setLevel(logging.DEBUG)
from AppKit import (
    NSAttributedString, NSBezierPath, NSColor, NSFont, NSFontAttributeName,
    NSForegroundColorAttributeName, NSImage, NSMutableAttributedString,
    NSTextAttachment, NSTextAttachmentCell,
)
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
PROVIDER_LETTERS = {"microsoft": "M", "google": "G", "okta": "O"}

# ── Jira (credential-based, non-OAuth) ──────────────────────────────────────

JIRA_SETTINGS_FIELDS = [
    ("jira_url", "Jira URL:"),
    ("jira_email", "Email:"),
    ("jira_api_token", "API Token:"),
    ("jira_project", "Project:"),
]
JIRA_ENV_DEFAULTS = {
    "jira_url": "JIRA_URL",
    "jira_email": "JIRA_EMAIL",
    "jira_api_token": "JIRA_API_TOKEN",
    "jira_project": "JIRA_PROJECT",
}

# ── Edit provider registry ──────────────────────────────────────────────────

EDIT_PROVIDERS = {
    "microsoft_edit": {
        "label": "Microsoft Edit",
        "letter": "M",
        "settings_fields": [
            ("ms_edit_tenant_id", "Tenant ID:"),
            ("ms_edit_client_id", "Client ID:"),
        ],
        "settings_defaults_from": {
            "ms_edit_tenant_id": "ms_tenant_id",
            "ms_edit_client_id": "ms_client_id",
        },
        "env_defaults": {
            "ms_edit_tenant_id": "AZURE_TENANT_ID",
            "ms_edit_client_id": "AZURE_CLIENT_ID",
        },
        "scopes": ["User.Read", "Files.ReadWrite.All", "Sites.ReadWrite.All"],
    },
    "google_edit": {
        "label": "Google Edit",
        "letter": "G",
        "settings_fields": [
            ("google_edit_client_id", "Client ID:"),
            ("google_edit_client_secret", "Client Secret:"),
        ],
        "settings_defaults_from": {
            "google_edit_client_id": "google_client_id",
            "google_edit_client_secret": "google_client_secret",
        },
        "env_defaults": {
            "google_edit_client_id": "GOOGLE_CLIENT_ID",
            "google_edit_client_secret": "GOOGLE_CLIENT_SECRET",
        },
        "scopes": (
            "openid email profile"
            " https://www.googleapis.com/auth/spreadsheets"
            " https://www.googleapis.com/auth/documents"
            " https://www.googleapis.com/auth/presentations"
            " https://www.googleapis.com/auth/drive"
        ),
    },
}

EDIT_PROVIDER_ORDER = ("microsoft_edit", "google_edit")

# ── Global config ────────────────────────────────────────────────────────────

REDIRECT_PORT = int(os.environ.get("REDIRECT_PORT", "53214"))
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}"
PROXY_PORT = int(os.environ.get("PROXY_PORT", "18765"))
JIRA_MCP_PORT = int(os.environ.get("JIRA_MCP_PORT", "18767"))
VOITTA_RAG_URL = os.environ.get("VOITTA_RAG_URL", "https://rag.voitta.ai")
EDIT_PROXY_URL = os.environ.get("EDIT_PROXY_URL", "http://localhost:8000")
EDIT_MCP_ENV_PATH = os.environ.get("EDIT_MCP_ENV_PATH", os.path.expanduser("~/DEVEL/google_workspace_mcp/.env"))
JIRA_MCP_ENV_PATH = os.environ.get("JIRA_MCP_ENV_PATH", os.path.expanduser("~/DEVEL/mcp-atlassian/.env"))
GOOGLE_MCP_DIR = os.environ.get("GOOGLE_MCP_DIR", os.path.expanduser("~/DEVEL/google_workspace_mcp"))
JIRA_MCP_DIR = os.environ.get("JIRA_MCP_DIR", os.path.expanduser("~/DEVEL/mcp-atlassian"))
SETTINGS_PATH = Path.home() / ".voitta_auth_settings.json"


# ── Jira URL helpers ─────────────────────────────────────────────────────────

def _parse_jira_url(url):
    """Parse a Jira URL into (base_url, project_key).

    Supports:
      https://org.atlassian.net/jira/software/projects/PROJ/issues/...
      https://jira.example.com/browse/PROJ
      https://jira.example.com/browse/PROJ-123
      https://jira.example.com/projects/PROJ
      Also extracts project from JQL query param.
    """
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    path_parts = [p for p in (parsed.path or "").strip("/").split("/") if p]

    project_key = ""
    for i, part in enumerate(path_parts):
        if part in ("projects", "browse") and i + 1 < len(path_parts):
            key_part = path_parts[i + 1]
            project_key = key_part.split("-")[0].upper()
            break

    # Fallback: try project key from JQL in query params
    if not project_key:
        qs = parse_qs(parsed.query)
        jql = qs.get("jql", [""])[0]
        if jql:
            m = re.search(r'project\s*=\s*["\']?(\w+)', jql)
            if m:
                project_key = m.group(1).upper()

    return base_url, project_key


def _fetch_jira_projects(base_url, email, token):
    """Fetch available Jira Cloud projects. Returns list of (key, name) tuples."""
    cred = base64.b64encode(f"{email}:{token}".encode()).decode()
    headers = {"Authorization": f"Basic {cred}", "Content-Type": "application/json"}
    projects = []
    start_at = 0
    while True:
        try:
            resp = requests.get(
                f"{base_url}/rest/api/3/project/search",
                params={"startAt": start_at, "maxResults": 50},
                headers=headers,
                timeout=15,
            )
            if resp.status_code != 200:
                print(f"[jira] Failed to fetch projects: {resp.status_code} {resp.text[:200]}")
                break
            data = resp.json()
            for p in data.get("values", []):
                projects.append((p["key"], p.get("name", p["key"])))
            if data.get("isLast", True):
                break
            start_at += len(data.get("values", []))
        except Exception as e:
            print(f"[jira] Error fetching projects: {e}")
            break
    projects.sort()
    return projects


class _JiraFetchHelper(NSObject):
    """Button action handler for fetching Jira projects inside the settings dialog."""

    def doFetch_(self, sender):
        url = self._url_field.stringValue().strip()
        email = self._email_field.stringValue().strip()
        token = self._token_field.stringValue().strip()
        if not (url and email and token):
            self._popup.removeAllItems()
            self._popup.addItemWithTitle_("(enter URL, email, and token first)")
            return

        server_url, parsed_project = _parse_jira_url(url)
        default_proj = self._default_project or parsed_project
        projects = _fetch_jira_projects(server_url, email, token)

        self._popup.removeAllItems()
        if not projects:
            self._popup.addItemWithTitle_("(no projects found — check credentials)")
        else:
            selected_idx = 0
            for i, (key, name) in enumerate(projects):
                self._popup.addItemWithTitle_(f"{key} — {name}")
                if key == default_proj:
                    selected_idx = i
            self._popup.selectItemAtIndex_(selected_idx)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _pkce_pair():
    """Generate PKCE code_verifier and code_challenge (S256)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def _circled_letter_image(letter, size, color):
    """Create an NSImage of a letter inside a circle, for menu bar use."""
    img = NSImage.alloc().initWithSize_((size, size))
    img.lockFocus()
    circle_rect = ((1, 1), (size - 2, size - 2))
    path = NSBezierPath.bezierPathWithOvalInRect_(circle_rect)
    color.set()
    path.setLineWidth_(1.2)
    path.stroke()
    font = NSFont.menuBarFontOfSize_(size * 0.5)
    attrs = {
        NSFontAttributeName: font,
        NSForegroundColorAttributeName: color,
    }
    text = NSAttributedString.alloc().initWithString_attributes_(letter, attrs)
    text_size = text.size()
    x = (size - text_size.width) / 2
    y = (size - text_size.height) / 2
    text.drawAtPoint_((x, y))
    img.unlockFocus()
    return img


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



# ── Main application ─────────────────────────────────────────────────────────

class VoittaAuthApp(rumps.App):
    def __init__(self):
        super().__init__("Voitta", title="M G O J")

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
        for key in EDIT_PROVIDER_ORDER:
            self._auth[key] = {
                "token": None,
                "refresh_token": None,
                "profile": None,
                "refresh_timer": None,
                "msal_app": None,
            }

        # Load persistent settings
        self._settings = self._load_settings()
        self.voitta_rag_url = self._settings.get("voitta_rag_url", VOITTA_RAG_URL)
        self.edit_proxy_url = self._settings.get("edit_proxy_url", EDIT_PROXY_URL)
        self.edit_mcp_env_path = self._settings.get("edit_mcp_env_path", EDIT_MCP_ENV_PATH)
        self.jira_mcp_env_path = self._settings.get("jira_mcp_env_path", JIRA_MCP_ENV_PATH)

        # Load per-provider credentials from settings (fall back to env vars)
        for key, cfg in PROVIDERS.items():
            for settings_key, env_var in cfg["env_defaults"].items():
                if settings_key not in self._settings:
                    self._settings[settings_key] = os.environ.get(env_var, "")

        # Load edit provider credentials (fall back to env vars, then base provider)
        for key, cfg in EDIT_PROVIDERS.items():
            for settings_key, env_var in cfg["env_defaults"].items():
                if settings_key not in self._settings:
                    env_val = os.environ.get(env_var, "")
                    base_key = cfg["settings_defaults_from"].get(settings_key, "")
                    self._settings[settings_key] = env_val or self._settings.get(base_key, "")

        # Load Jira credentials (fall back to env vars)
        for settings_key, env_var in JIRA_ENV_DEFAULTS.items():
            if settings_key not in self._settings:
                self._settings[settings_key] = os.environ.get(env_var, "")

        # Initialize MSAL for Microsoft (read + edit)
        self._rebuild_msal_app()
        self._rebuild_msal_app_edit()

        # Sync Google Edit credentials to the workspace MCP server's .env
        self._sync_edit_mcp_env()

        # Sync Jira credentials to the mcp-atlassian server's .env
        self._sync_jira_mcp_env()

        # Launch MCP server subprocesses (after .env sync)
        self._start_mcp_subprocesses()

        # Build menu with direct references
        self._menu_items = {}
        menu_list = []
        for key in PROVIDER_ORDER:
            label = PROVIDERS[key]["label"]
            item = rumps.MenuItem(f"Activate {label}", callback=self._make_toggle_callback(key))
            self._menu_items[key] = item
            menu_list.append(item)
        menu_list.append(None)  # separator
        for key in EDIT_PROVIDER_ORDER:
            label = EDIT_PROVIDERS[key]["label"]
            item = rumps.MenuItem(f"Activate {label}", callback=self._make_edit_toggle_callback(key))
            self._menu_items[key] = item
            menu_list.append(item)
        menu_list.append(None)  # separator
        jira_item = rumps.MenuItem("Jira: Not Configured", callback=self.show_settings)
        self._menu_items["jira"] = jira_item
        menu_list.append(jira_item)
        menu_list.append(None)  # separator
        menu_list.append(rumps.MenuItem("Settings", callback=self.show_settings))
        menu_list.append(rumps.MenuItem("Help", callback=self.show_help))
        self.menu = menu_list

        self._update_menu_state()

        # Start unified FastMCP proxy server
        threading.Thread(target=self._run_fastmcp_proxy, daemon=True).start()

    # ── Menu state ───────────────────────────────────────────────────────────

    def _build_title(self):
        parts = [PROVIDER_LETTERS[k] for k in PROVIDER_ORDER]
        parts.append("J")
        return " ".join(parts)

    def _apply_attributed_title(self):
        """Set menu bar title with dimmed/bright letters + circled edit indicators."""
        try:
            button = self._nsapp.nsstatusitem.button()
        except AttributeError:
            return
        is_dark = "Dark" in str(button.effectiveAppearance().name())
        base = 1.0 if is_dark else 0.0
        font = NSFont.menuBarFontOfSize_(0)
        title = NSMutableAttributedString.alloc().init()

        # RAG provider letters (M G O)
        for i, key in enumerate(PROVIDER_ORDER):
            if i > 0:
                space = NSAttributedString.alloc().initWithString_attributes_(
                    " ", {NSFontAttributeName: font}
                )
                title.appendAttributedString_(space)
            active = self._auth[key]["token"] is not None
            alpha = 1.0 if active else 0.4
            color = NSColor.colorWithCalibratedWhite_alpha_(base, alpha)
            attrs = {
                NSForegroundColorAttributeName: color,
                NSFontAttributeName: font,
            }
            char = NSAttributedString.alloc().initWithString_attributes_(
                PROVIDER_LETTERS[key], attrs
            )
            title.appendAttributedString_(char)

        # Jira letter (J) — bright if credentials configured, dim otherwise
        space = NSAttributedString.alloc().initWithString_attributes_(
            " ", {NSFontAttributeName: font}
        )
        title.appendAttributedString_(space)
        jira_active = self._has_jira_credentials()
        alpha = 1.0 if jira_active else 0.4
        color = NSColor.colorWithCalibratedWhite_alpha_(base, alpha)
        attrs = {
            NSForegroundColorAttributeName: color,
            NSFontAttributeName: font,
        }
        j_char = NSAttributedString.alloc().initWithString_attributes_("J", attrs)
        title.appendAttributedString_(j_char)

        # Edit provider circled letters
        any_edit = any(self._has_edit_credentials(k) for k in EDIT_PROVIDER_ORDER)
        if any_edit:
            space = NSAttributedString.alloc().initWithString_attributes_(
                "  ", {NSFontAttributeName: font}
            )
            title.appendAttributedString_(space)

            icon_size = font.pointSize() + 2
            for j, key in enumerate(EDIT_PROVIDER_ORDER):
                if not self._has_edit_credentials(key):
                    continue
                if j > 0:
                    sp = NSAttributedString.alloc().initWithString_attributes_(
                        " ", {NSFontAttributeName: font}
                    )
                    title.appendAttributedString_(sp)
                active = self._auth[key]["token"] is not None
                alpha = 1.0 if active else 0.4
                color = NSColor.colorWithCalibratedWhite_alpha_(base, alpha)
                img = _circled_letter_image(EDIT_PROVIDERS[key]["letter"], icon_size, color)
                attachment = NSTextAttachment.alloc().init()
                cell = NSTextAttachmentCell.alloc().initImageCell_(img)
                attachment.setAttachmentCell_(cell)
                img_str = NSAttributedString.attributedStringWithAttachment_(attachment)
                title.appendAttributedString_(img_str)

        button.setAttributedTitle_(title)

    @rumps.timer(0.1)
    def _startup_title(self, timer):
        """Apply attributed title once the status bar is ready."""
        self._apply_attributed_title()
        timer.stop()

    def _has_edit_credentials(self, key):
        """Return True if the edit provider has required credentials configured."""
        cfg = EDIT_PROVIDERS[key]
        for settings_key, _ in cfg["settings_fields"]:
            val = self._settings.get(settings_key, "")
            if not val:
                base_key = cfg["settings_defaults_from"].get(settings_key, "")
                if not self._settings.get(base_key, ""):
                    return False
        return True

    def _has_jira_credentials(self):
        """Return True if Jira is fully configured (URL, email, token, and project)."""
        return (
            bool(self._settings.get("jira_url", ""))
            and bool(self._settings.get("jira_email", ""))
            and bool(self._settings.get("jira_api_token", ""))
            and bool(self._settings.get("jira_project", ""))
        )

    def _log_jira_projects(self):
        """Fetch and log available Jira projects in the background."""
        server_url = self._settings.get("jira_server_url", "")
        email = self._settings.get("jira_email", "")
        token = self._settings.get("jira_api_token", "")
        if not (server_url and email and token):
            return
        projects = _fetch_jira_projects(server_url, email, token)
        if projects:
            names = ", ".join(f"{k} ({n})" for k, n in projects)
            print(f"[jira] Available projects: {names}")
        else:
            print("[jira] No projects found or failed to fetch")

    def _update_menu_state(self):
        self.title = self._build_title()
        self._apply_attributed_title()
        for key in PROVIDER_ORDER:
            label = PROVIDERS[key]["label"]
            item = self._menu_items[key]
            if self._auth[key]["token"]:
                item.title = f"Deactivate {label}"
            else:
                item.title = f"Activate {label}"
        for key in EDIT_PROVIDER_ORDER:
            label = EDIT_PROVIDERS[key]["label"]
            item = self._menu_items[key]
            if self._auth[key]["token"]:
                item.title = f"Deactivate {label}"
            else:
                item.title = f"Activate {label}"
        # Jira status
        if "jira" in self._menu_items:
            if self._has_jira_credentials():
                email = self._settings.get("jira_email", "")
                project = self._settings.get("jira_project", "")
                if project:
                    self._menu_items["jira"].title = f"Jira: {project} ({email})"
                else:
                    self._menu_items["jira"].title = f"Jira: {email}"
            else:
                self._menu_items["jira"].title = "Jira: Not Configured"

    def _make_toggle_callback(self, provider_key):
        def callback(_):
            if self._auth[provider_key]["token"]:
                self._deauth_provider(provider_key)
            else:
                threading.Thread(
                    target=self._do_auth, args=(provider_key,), daemon=True
                ).start()
        return callback

    def _make_edit_toggle_callback(self, provider_key):
        def callback(_):
            if self._auth[provider_key]["token"]:
                self._deauth_edit_provider(provider_key)
            else:
                threading.Thread(
                    target=self._do_auth_edit, args=(provider_key,), daemon=True
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

    def _rebuild_msal_app_edit(self):
        tenant_id = self._settings.get("ms_edit_tenant_id", "") or self._settings.get("ms_tenant_id", "")
        client_id = self._settings.get("ms_edit_client_id", "") or self._settings.get("ms_client_id", "")
        if tenant_id and client_id:
            self._auth["microsoft_edit"]["msal_app"] = msal.PublicClientApplication(
                client_id,
                authority=f"https://login.microsoftonline.com/{tenant_id}",
            )
        else:
            self._auth["microsoft_edit"]["msal_app"] = None

    # ── Edit MCP .env sync ────────────────────────────────────────────────────

    def _sync_edit_mcp_env(self):
        """Write Google Edit credentials to the workspace MCP server's .env file."""
        env_path = self.edit_mcp_env_path
        if not env_path:
            return

        client_id = (self._settings.get("google_edit_client_id", "")
                     or self._settings.get("google_client_id", ""))
        client_secret = (self._settings.get("google_edit_client_secret", "")
                         or self._settings.get("google_client_secret", ""))

        if not client_id or not client_secret:
            print("[voitta-auth] Skipping edit MCP .env sync — no Google credentials")
            return

        lines = [
            "# Managed by voitta-auth — do not edit manually",
            f"GOOGLE_OAUTH_CLIENT_ID={client_id}",
            f"GOOGLE_OAUTH_CLIENT_SECRET={client_secret}",
            "MCP_ENABLE_OAUTH21=true",
            "EXTERNAL_OAUTH21_PROVIDER=true",
            "",
        ]
        try:
            Path(env_path).parent.mkdir(parents=True, exist_ok=True)
            with open(env_path, "w") as f:
                f.write("\n".join(lines))
            print(f"[voitta-auth] Wrote edit MCP .env → {env_path}")
        except Exception as e:
            print(f"[voitta-auth] Failed to write edit MCP .env: {e}")

    # ── Jira MCP .env sync ───────────────────────────────────────────────────

    def _sync_jira_mcp_env(self):
        """Write Jira credentials to the mcp-atlassian server's .env file."""
        env_path = self.jira_mcp_env_path
        if not env_path:
            return

        jira_url = self._settings.get("jira_server_url", "")
        if not jira_url:
            # Try parsing from jira_url setting
            raw_url = self._settings.get("jira_url", "")
            if raw_url:
                jira_url, _ = _parse_jira_url(raw_url)
        email = self._settings.get("jira_email", "")
        token = self._settings.get("jira_api_token", "")

        if not jira_url or not email or not token:
            print("[voitta-auth] Skipping Jira MCP .env sync — missing credentials")
            return

        project = self._settings.get("jira_project", "")

        lines = [
            "# Managed by voitta-auth — do not edit manually",
            f"JIRA_URL={jira_url}",
            f"JIRA_USERNAME={email}",
            f"JIRA_API_TOKEN={token}",
        ]
        if project:
            lines.append(f"JIRA_PROJECTS_FILTER={project}")
        lines.append("")

        try:
            Path(env_path).parent.mkdir(parents=True, exist_ok=True)
            with open(env_path, "w") as f:
                f.write("\n".join(lines))
            print(f"[voitta-auth] Wrote Jira MCP .env → {env_path}")
        except Exception as e:
            print(f"[voitta-auth] Failed to write Jira MCP .env: {e}")

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

    # ── Edit auth dispatcher ────────────────────────────────────────────────

    def _do_auth_edit(self, provider_key):
        if not self._auth_lock.acquire(blocking=False):
            _notify("Voitta Auth", "Busy", "Another authentication is in progress.")
            return
        try:
            label = EDIT_PROVIDERS[provider_key]["label"]
            print(f"[voitta-auth] Starting {label} OAuth2 flow...")
            if provider_key == "microsoft_edit":
                self._do_auth_microsoft_edit()
            elif provider_key == "google_edit":
                self._do_auth_google_edit()
        except Exception as e:
            print(f"[voitta-auth] {provider_key} EXCEPTION: {e}")
            traceback.print_exc()
            _notify("Voitta Auth", "Error", str(e))
        finally:
            self._auth_lock.release()

    # ── Microsoft Edit auth ─────────────────────────────────────────────────

    def _do_auth_microsoft_edit(self):
        state = self._auth["microsoft_edit"]
        msal_app = state["msal_app"]
        if not msal_app:
            _notify("Voitta Auth", "Microsoft Edit", "Configure Tenant ID and Client ID in Settings first.")
            return

        scopes = EDIT_PROVIDERS["microsoft_edit"]["scopes"]
        auth_url = msal_app.get_authorization_request_url(
            scopes, redirect_uri=REDIRECT_URI
        )
        webbrowser.open(auth_url)
        code, error = self._wait_for_callback()

        if not code:
            _notify("Voitta Auth", "Microsoft Edit", error or "No authorization code received.")
            return

        print("[voitta-auth] Got Microsoft Edit auth code, exchanging for token...")
        result = msal_app.acquire_token_by_authorization_code(
            code, scopes=scopes, redirect_uri=REDIRECT_URI
        )

        if "access_token" in result:
            state["token"] = result["access_token"]
            self._fetch_profile_microsoft_edit()
            self._schedule_refresh("microsoft_edit", result.get("expires_in", 3600))
            name = state["profile"].get("name", "Unknown") if state["profile"] else "Unknown"
            print(f"[voitta-auth] Microsoft Edit authenticated as {name}")
            self._update_menu_state()
            _notify("Voitta Auth", "Microsoft Edit", f"Welcome, {name}!")
        else:
            error = result.get("error_description", result.get("error", "Unknown error"))
            print(f"[voitta-auth] Microsoft Edit token exchange failed: {error}")
            _notify("Voitta Auth", "Microsoft Edit", str(error))

    def _fetch_profile_microsoft_edit(self):
        state = self._auth["microsoft_edit"]
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

    # ── Google Edit auth ────────────────────────────────────────────────────

    def _do_auth_google_edit(self):
        client_id = self._settings.get("google_edit_client_id", "") or self._settings.get("google_client_id", "")
        client_secret = self._settings.get("google_edit_client_secret", "") or self._settings.get("google_client_secret", "")
        if not client_id or not client_secret:
            _notify("Voitta Auth", "Google Edit", "Configure Client ID and Client Secret in Settings first.")
            return

        verifier, challenge = _pkce_pair()
        params = {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": EDIT_PROVIDERS["google_edit"]["scopes"],
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
        }
        auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
        webbrowser.open(auth_url)
        code, error = self._wait_for_callback()

        if not code:
            _notify("Voitta Auth", "Google Edit", error or "No authorization code received.")
            return

        print("[voitta-auth] Got Google Edit auth code, exchanging for token...")
        resp = requests.post("https://oauth2.googleapis.com/token", data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": REDIRECT_URI,
        }, timeout=10)

        if not resp.ok:
            _notify("Voitta Auth", "Google Edit", f"Token exchange failed: {resp.text[:200]}")
            return

        data = resp.json()
        state = self._auth["google_edit"]
        state["token"] = data["access_token"]
        state["refresh_token"] = data.get("refresh_token")
        self._fetch_profile_google_edit()
        self._schedule_refresh("google_edit", data.get("expires_in", 3600))
        name = state["profile"].get("name", "Unknown") if state["profile"] else "Unknown"
        print(f"[voitta-auth] Google Edit authenticated as {name}")
        self._update_menu_state()
        self._sync_edit_mcp_env()
        _notify("Voitta Auth", "Google Edit", f"Welcome, {name}!")

    def _fetch_profile_google_edit(self):
        state = self._auth["google_edit"]
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

    # ── Token refresh ────────────────────────────────────────────────────────

    def _schedule_refresh(self, provider_key, expires_in):
        state = self._auth[provider_key]
        if state["refresh_timer"]:
            state["refresh_timer"].cancel()
        refresh_in = max(expires_in - 300, 60)

        if provider_key == "microsoft":
            timer = threading.Timer(refresh_in, self._do_refresh_microsoft)
        elif provider_key == "microsoft_edit":
            timer = threading.Timer(refresh_in, self._do_refresh_microsoft_edit)
        elif provider_key in ("google", "google_edit"):
            timer = threading.Timer(refresh_in, self._do_refresh_generic,
                                    args=(provider_key, "https://oauth2.googleapis.com/token"))
        elif provider_key == "okta":
            domain = self._settings.get("okta_domain", "")
            timer = threading.Timer(refresh_in, self._do_refresh_generic,
                                    args=("okta", f"https://{domain}/oauth2/default/v1/token"))
        else:
            return

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

    def _do_refresh_microsoft_edit(self):
        state = self._auth["microsoft_edit"]
        msal_app = state["msal_app"]
        if not msal_app:
            return
        accounts = msal_app.get_accounts()
        if not accounts:
            return
        scopes = EDIT_PROVIDERS["microsoft_edit"]["scopes"]
        result = msal_app.acquire_token_silent(scopes, account=accounts[0], force_refresh=True)
        if result and "access_token" in result:
            state["token"] = result["access_token"]
            self._schedule_refresh("microsoft_edit", result.get("expires_in", 3600))
            print("[voitta-auth] Microsoft Edit token refreshed silently")
        else:
            print("[voitta-auth] Microsoft Edit silent refresh failed — user must re-authenticate")
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
        elif provider_key == "google_edit":
            client_id = self._settings.get("google_edit_client_id", "") or self._settings.get("google_client_id", "")
            client_secret = self._settings.get("google_edit_client_secret", "") or self._settings.get("google_client_secret", "")
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

    def _deauth_edit_provider(self, provider_key):
        state = self._auth[provider_key]
        if state["refresh_timer"]:
            state["refresh_timer"].cancel()
            state["refresh_timer"] = None

        if provider_key == "microsoft_edit" and state["msal_app"]:
            for account in state["msal_app"].get_accounts():
                state["msal_app"].remove_account(account)

        state["token"] = None
        state["refresh_token"] = None
        state["profile"] = None

        label = EDIT_PROVIDERS[provider_key]["label"]
        print(f"[voitta-auth] {label} signed out")
        _notify("Voitta Auth", label, "Signed out.")
        self._update_menu_state()

    # ── FastMCP Proxy ─────────────────────────────────────────────────────────

    def _make_rag_client_factory(self):
        """Return a factory that creates a ProxyClient with current RAG auth headers."""
        app = self
        def factory():
            headers = {}
            for key in PROVIDER_ORDER:
                state = app._auth[key]
                if not state["token"]:
                    continue
                suffix = PROVIDERS[key]["label"]
                headers[f"X-Auth-Token-{suffix}"] = f"Bearer {state['token']}"
                if state["profile"]:
                    headers[f"X-Auth-Email-{suffix}"] = state["profile"].get("email", "")
                    headers[f"X-Auth-Name-{suffix}"] = state["profile"].get("name", "")
            url = f"{app.voitta_rag_url.rstrip('/')}/mcp/mcp"
            print(f"[voitta-auth] RAG factory: url={url}, {len(headers)} headers")
            transport = StreamableHttpTransport(url=url, headers=headers)
            return ProxyClient(transport)
        return factory

    def _make_google_client_factory(self):
        """Return a factory that creates a ProxyClient with current edit Bearer token."""
        app = self
        def factory():
            headers = {}
            for key in EDIT_PROVIDER_ORDER:
                state = app._auth[key]
                if state["token"]:
                    headers["Authorization"] = f"Bearer {state['token']}"
                    if state["profile"]:
                        headers["X-Auth-Email"] = state["profile"].get("email", "")
                        headers["X-Auth-Name"] = state["profile"].get("name", "")
                    break
            url = f"{app.edit_proxy_url.rstrip('/')}/mcp"
            print(f"[voitta-auth] Google factory: url={url}, headers={list(headers.keys())}")
            transport = StreamableHttpTransport(url=url, headers=headers)
            return ProxyClient(transport)
        return factory

    def _run_fastmcp_proxy(self):
        """Run unified FastMCP proxy server mounting all backends."""
        main_server = FastMCPServer("voitta-auth")

        # RAG proxy with dynamic per-provider auth headers
        rag_proxy = FastMCPProxy(
            client_factory=self._make_rag_client_factory(),
            name="voitta-rag",
        )
        main_server.mount(rag_proxy, prefix="voitta_rag")

        # Google Workspace proxy with dynamic Bearer token
        google_proxy = FastMCPProxy(
            client_factory=self._make_google_client_factory(),
            name="google-sheets",
        )
        main_server.mount(google_proxy, prefix="google_sheets")

        # Jira proxy (credentials already in subprocess .env)
        jira_proxy = FastMCPServer.as_proxy(
            f"http://localhost:{JIRA_MCP_PORT}/mcp",
            name="jira",
        )
        main_server.mount(jira_proxy, prefix="jira")

        print(f"[voitta-auth] FastMCP proxy on http://127.0.0.1:{PROXY_PORT}/mcp")
        print(f"[voitta-auth]   RAG → {self.voitta_rag_url}")
        print(f"[voitta-auth]   Google → {self.edit_proxy_url}")
        print(f"[voitta-auth]   Jira → http://localhost:{JIRA_MCP_PORT}/mcp")
        main_server.run(transport="streamable-http", host="127.0.0.1", port=PROXY_PORT)

    # ── MCP subprocess management ────────────────────────────────────────────

    def _start_mcp_subprocesses(self):
        """Launch google_workspace_mcp and mcp-atlassian as background processes."""
        self._subprocesses = []

        # Google Workspace MCP
        if Path(GOOGLE_MCP_DIR).is_dir():
            try:
                proc = subprocess.Popen(
                    ["uv", "run", "main.py", "--transport", "streamable-http"],
                    cwd=GOOGLE_MCP_DIR,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._subprocesses.append(proc)
                print(f"[voitta-auth] Started google_workspace_mcp (pid {proc.pid}) in {GOOGLE_MCP_DIR}")
            except Exception as e:
                print(f"[voitta-auth] Failed to start google_workspace_mcp: {e}")
        else:
            print(f"[voitta-auth] Skipping google_workspace_mcp — {GOOGLE_MCP_DIR} not found")

        # mcp-atlassian (Jira/Confluence)
        env_file = self.jira_mcp_env_path
        if Path(JIRA_MCP_DIR).is_dir() and env_file and Path(env_file).exists():
            try:
                proc = subprocess.Popen(
                    [
                        "uvx", "mcp-atlassian",
                        "--transport", "streamable-http",
                        "--port", str(JIRA_MCP_PORT),
                        "--env-file", env_file,
                    ],
                    cwd=JIRA_MCP_DIR,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._subprocesses.append(proc)
                print(f"[voitta-auth] Started mcp-atlassian (pid {proc.pid}) on port {JIRA_MCP_PORT}")
            except Exception as e:
                print(f"[voitta-auth] Failed to start mcp-atlassian: {e}")
        else:
            print(f"[voitta-auth] Skipping mcp-atlassian — {JIRA_MCP_DIR} or {env_file} not found")

        atexit.register(self._stop_mcp_subprocesses)

    def _stop_mcp_subprocesses(self):
        """Terminate all managed MCP subprocesses."""
        for proc in getattr(self, "_subprocesses", []):
            if proc.poll() is None:
                print(f"[voitta-auth] Terminating subprocess pid {proc.pid}")
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

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
        data = {
            "voitta_rag_url": self.voitta_rag_url,
            "edit_proxy_url": self.edit_proxy_url,
            "edit_mcp_env_path": self.edit_mcp_env_path,
            "jira_mcp_env_path": self.jira_mcp_env_path,
        }
        for cfg in PROVIDERS.values():
            for settings_key, _ in cfg["settings_fields"]:
                data[settings_key] = self._settings.get(settings_key, "")
        for cfg in EDIT_PROVIDERS.values():
            for settings_key, _ in cfg["settings_fields"]:
                data[settings_key] = self._settings.get(settings_key, "")
        for settings_key, _ in JIRA_SETTINGS_FIELDS:
            data[settings_key] = self._settings.get(settings_key, "")
        # Also persist derived Jira server URL
        if self._settings.get("jira_server_url"):
            data["jira_server_url"] = self._settings["jira_server_url"]
        with open(SETTINGS_PATH, "w") as f:
            json.dump(data, f, indent=2)
        print("[voitta-auth] Settings saved")

    def show_settings(self, _):
        from AppKit import (
            NSApp, NSAlert, NSButton, NSFont,
            NSPopUpButton, NSTextField, NSView,
        )
        from Foundation import NSMakeRect

        label_w, field_w, row_h, header_h, gap = 140, 512, 22, 20, 8
        total_w = label_w + field_w

        # Build row descriptors: (label, value_or_None, settings_key_or_None, is_header)
        rows = [("Voitta RAG URL:", self.voitta_rag_url, "voitta_rag_url", False)]
        for key in PROVIDER_ORDER:
            cfg = PROVIDERS[key]
            rows.append((f"── {cfg['label']} ──", None, None, True))
            for settings_key, field_label in cfg["settings_fields"]:
                rows.append((field_label, self._settings.get(settings_key, ""), settings_key, False))

        rows.append(("Edit Proxy URL:", self.edit_proxy_url, "edit_proxy_url", False))
        rows.append(("Edit MCP .env:", self.edit_mcp_env_path, "edit_mcp_env_path", False))
        for key in EDIT_PROVIDER_ORDER:
            cfg = EDIT_PROVIDERS[key]
            rows.append((f"── {cfg['label']} ──", None, None, True))
            for settings_key, field_label in cfg["settings_fields"]:
                val = self._settings.get(settings_key, "")
                if not val:
                    base_key = cfg["settings_defaults_from"].get(settings_key, "")
                    val = self._settings.get(base_key, "")
                rows.append((field_label, val, settings_key, False))

        rows.append(("Jira MCP .env:", self.jira_mcp_env_path, "jira_mcp_env_path", False))
        rows.append(("── Jira ──", None, None, True))
        # Jira URL/Email/Token as text fields; Project handled as popup below
        jira_url_val = self._settings.get("jira_url", "")
        parsed_project = ""
        if jira_url_val:
            _, parsed_project = _parse_jira_url(jira_url_val)
        for settings_key, field_label in JIRA_SETTINGS_FIELDS:
            if settings_key == "jira_project":
                continue  # rendered as popup + fetch button below
            rows.append((field_label, self._settings.get(settings_key, ""), settings_key, False))

        # Calculate total height (generic rows + 1 extra row for popup+button)
        total_h = 0
        for i, (_, _, _, is_header) in enumerate(rows):
            total_h += header_h if is_header else row_h
            if i > 0:
                total_h += gap
        total_h += row_h + gap  # for "Project: [popup] [Fetch]" row

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

        # ── Jira project popup + Fetch button ─────────────────────────────
        y_cursor -= row_h
        lbl = NSTextField.alloc().initWithFrame_(
            NSMakeRect(0, y_cursor + 1, label_w - 6, row_h)
        )
        lbl.setStringValue_("Project:")
        lbl.setBezeled_(False)
        lbl.setDrawsBackground_(False)
        lbl.setEditable_(False)
        lbl.setSelectable_(False)
        lbl.setAlignment_(1)
        container.addSubview_(lbl)

        popup_w = field_w - 130
        jira_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(label_w, y_cursor, popup_w, row_h), False
        )
        stored_project = self._settings.get("jira_project", "")
        if stored_project:
            jira_popup.addItemWithTitle_(stored_project)
        else:
            jira_popup.addItemWithTitle_("(click Fetch Projects)")
        container.addSubview_(jira_popup)

        fetch_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(label_w + popup_w + 8, y_cursor, 122, row_h)
        )
        fetch_btn.setTitle_("Fetch Projects")
        fetch_btn.setBezelStyle_(1)  # NSRoundedBezelStyle
        container.addSubview_(fetch_btn)

        # Wire fetch helper to button
        jira_tf = {k: f for k, f in text_fields if k.startswith("jira_")}
        fetch_helper = _JiraFetchHelper.alloc().init()
        fetch_helper._url_field = jira_tf.get("jira_url")
        fetch_helper._email_field = jira_tf.get("jira_email")
        fetch_helper._token_field = jira_tf.get("jira_api_token")
        fetch_helper._popup = jira_popup
        fetch_helper._btn = fetch_btn
        fetch_helper._default_project = parsed_project or stored_project
        fetch_btn.setTarget_(fetch_helper)
        fetch_btn.setAction_("doFetch:")

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

        for settings_key, fld in text_fields:
            val = fld.stringValue().strip()
            if settings_key == "voitta_rag_url":
                self.voitta_rag_url = val.rstrip("/") or self.voitta_rag_url
            elif settings_key == "edit_proxy_url":
                self.edit_proxy_url = val.rstrip("/") or self.edit_proxy_url
            elif settings_key == "edit_mcp_env_path":
                self.edit_mcp_env_path = val or self.edit_mcp_env_path
            elif settings_key == "jira_mcp_env_path":
                self.jira_mcp_env_path = val or self.jira_mcp_env_path
            else:
                self._settings[settings_key] = val

        # Collect Jira project from popup
        selected = jira_popup.titleOfSelectedItem()
        if selected and not selected.startswith("("):
            self._settings["jira_project"] = selected.split(" — ")[0].strip()
        else:
            self._settings["jira_project"] = ""

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

        for key, cfg in EDIT_PROVIDERS.items():
            changed = False
            for settings_key, _ in cfg["settings_fields"]:
                if self._settings.get(settings_key, "") != old_settings.get(settings_key, ""):
                    changed = True
                    break
            if changed:
                self._deauth_edit_provider(key)
                if key == "microsoft_edit":
                    self._rebuild_msal_app_edit()
                print(f"[voitta-auth] {cfg['label']} credentials changed — session cleared")

        self._sync_edit_mcp_env()

        # Detect Jira credential changes
        jira_changed = any(
            self._settings.get(k, "") != old_settings.get(k, "")
            for k, _ in JIRA_SETTINGS_FIELDS
        )
        if jira_changed:
            # Parse URL to extract server URL and default project
            jira_url = self._settings.get("jira_url", "")
            if jira_url:
                server_url, parsed_project = _parse_jira_url(jira_url)
                self._settings["jira_server_url"] = server_url
                if not self._settings.get("jira_project", "") and parsed_project:
                    self._settings["jira_project"] = parsed_project
                print(f"[voitta-auth] Jira server: {server_url}, project: {self._settings.get('jira_project', '')}")
            print("[voitta-auth] Jira credentials changed")
            self._save_settings()
            self._sync_jira_mcp_env()
            self._update_menu_state()
            # Fetch available projects in background
            if self._has_jira_credentials() and self._settings.get("jira_server_url"):
                threading.Thread(target=self._log_jira_projects, daemon=True).start()

    # ── Help ─────────────────────────────────────────────────────────────────

    def show_help(self, _):
        from AppKit import NSAlert
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Voitta Auth Help")
        alert.setInformativeText_(
            "Voitta Auth sits in your menu bar.\n\n"
            "Status: M G O J  (M) (G)\n"
            "  Bright = authenticated/configured, dimmed = not\n"
            "  Letters = RAG providers + Jira, circled = Edit providers\n\n"
            "Activate/Deactivate each provider independently.\n"
            "Jira: paste a Jira URL to auto-detect server and project.\n"
            "  Uses email + API token (no browser login).\n\n"
            f"MCP proxy: http://127.0.0.1:{PROXY_PORT}/mcp (unified)\n"
            f"  RAG → {self.voitta_rag_url}\n"
            f"  Google → {self.edit_proxy_url}\n"
            f"  Jira → http://127.0.0.1:{JIRA_MCP_PORT}/mcp\n"
            f"Google MCP dir: {GOOGLE_MCP_DIR}\n"
            f"Jira MCP dir: {JIRA_MCP_DIR}"
        )
        alert.addButtonWithTitle_("OK")
        _show_modal(alert)


if __name__ == "__main__":
    VoittaAuthApp().run()
