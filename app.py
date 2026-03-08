#!/usr/bin/env python3
"""Voitta Auth - macOS menu bar app for multi-provider OAuth2 authentication with MCP proxy."""

import asyncio
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
from fastmcp.server.providers.proxy import FastMCPProxy, ProxyClient, ProxyProvider, ProxyTool
from fastmcp.client.transports import StreamableHttpTransport
import mcp.types
from config import load_config, save_config, migrate_from_legacy, apps_for_backend, CONFIG_PATH

# Surface FastMCP proxy errors (otherwise silently swallowed at DEBUG)
logging.getLogger("fastmcp.server.providers.aggregate").setLevel(logging.DEBUG)
logging.basicConfig(level=logging.WARNING, format="[%(name)s] %(levelname)s: %(message)s")
logging.getLogger("fastmcp.server.providers.aggregate").setLevel(logging.DEBUG)

_proxy_logger = logging.getLogger("voitta-auth.proxy")


TOOL_CACHE_DIR = Path.home() / ".voitta_auth_cache"


def _cache_path(backend_name: str, kind: str) -> Path:
    """Return the JSON cache file path for a given backend and listing kind."""
    safe_name = re.sub(r"[^\w\-]", "_", backend_name).lower()
    return TOOL_CACHE_DIR / f"{safe_name}_{kind}.json"


def _proxy_tool_to_mcp_dict(item) -> dict:
    """Convert a ProxyTool (or any Tool) to an mcp.types.Tool-compatible dict."""
    d = item.model_dump()
    # ProxyTool uses 'parameters'/'output_schema'; mcp.types.Tool uses 'inputSchema'/'outputSchema'
    if "inputSchema" not in d and "parameters" in d:
        d["inputSchema"] = d.pop("parameters")
    if "outputSchema" not in d and "output_schema" in d:
        d["outputSchema"] = d.pop("output_schema")
    # Drop extra ProxyTool-only fields that mcp.types.Tool rejects
    for extra in ("version", "tags", "task_config", "serializer", "timeout"):
        d.pop(extra, None)
    return d


def _save_cache(backend_name: str, kind: str, items):
    """Serialize MCP objects to a JSON cache file."""
    try:
        TOOL_CACHE_DIR.mkdir(exist_ok=True)
        data = [_proxy_tool_to_mcp_dict(item) if kind == "tools" else item.model_dump()
                for item in items]
        _cache_path(backend_name, kind).write_text(
            json.dumps(data, default=lambda o: list(o) if isinstance(o, set) else str(o))
        )
        _proxy_logger.info("[%s] Cached %d %s to disk", backend_name, len(data), kind)
    except Exception as e:
        _proxy_logger.warning("[%s] Failed to write %s cache: %s", backend_name, kind, e)


def _load_cache(backend_name: str, kind: str, model_cls, client_factory=None):
    """Deserialize MCP objects from a JSON cache file, or return None on miss."""
    path = _cache_path(backend_name, kind)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if kind == "tools" and client_factory is not None:
            # Load as ProxyTool so run() forwards to upstream (not base Tool
            # which raises NotImplementedError). Cache stores mcp.types.Tool
            # format (inputSchema/outputSchema) which from_mcp_tool expects.
            return [
                ProxyTool.from_mcp_tool(client_factory, mcp.types.Tool.model_validate(item))
                for item in data
            ]
        return [model_cls.model_validate(item) for item in data]
    except Exception as e:
        _proxy_logger.warning("[%s] Failed to read %s cache: %s", backend_name, kind, e)
        return None


class ResilientProxyProvider(ProxyProvider):
    """ProxyProvider that catches all upstream errors instead of only McpError.

    When the upstream MCP server is unreachable (not started yet, crashed, etc.),
    tool/resource/prompt listing returns cached results (if available) or empty
    lists.  Successful listings are persisted to disk so they survive restarts.
    """

    def __init__(self, client_factory, *, backend_name: str = "upstream", cache_listings: bool = False):
        super().__init__(client_factory)
        self._backend_name = backend_name
        self._cache_listings = cache_listings

    async def _list_tools(self):
        try:
            tools = await super()._list_tools()
            if self._cache_listings and tools:
                _save_cache(self._backend_name, "tools", tools)
            return tools
        except Exception as e:
            _proxy_logger.warning("[%s] Upstream unavailable for tool listing: %s", self._backend_name, e)
            if self._cache_listings:
                cached = _load_cache(self._backend_name, "tools", mcp.types.Tool, client_factory=self.client_factory)
                if cached is not None:
                    _proxy_logger.info("[%s] Returning %d cached tools", self._backend_name, len(cached))
                    return cached
            return []

    async def _list_resources(self):
        try:
            resources = await super()._list_resources()
            if self._cache_listings and resources:
                _save_cache(self._backend_name, "resources", resources)
            return resources
        except Exception as e:
            _proxy_logger.warning("[%s] Upstream unavailable for resource listing: %s", self._backend_name, e)
            if self._cache_listings:
                cached = _load_cache(self._backend_name, "resources", mcp.types.Resource)
                if cached is not None:
                    _proxy_logger.info("[%s] Returning %d cached resources", self._backend_name, len(cached))
                    return cached
            return []

    async def _list_resource_templates(self):
        try:
            templates = await super()._list_resource_templates()
            if self._cache_listings and templates:
                _save_cache(self._backend_name, "templates", templates)
            return templates
        except Exception as e:
            _proxy_logger.warning("[%s] Upstream unavailable for template listing: %s", self._backend_name, e)
            if self._cache_listings:
                cached = _load_cache(self._backend_name, "templates", mcp.types.ResourceTemplate)
                if cached is not None:
                    _proxy_logger.info("[%s] Returning %d cached templates", self._backend_name, len(cached))
                    return cached
            return []

    async def _list_prompts(self):
        try:
            prompts = await super()._list_prompts()
            if self._cache_listings and prompts:
                _save_cache(self._backend_name, "prompts", prompts)
            return prompts
        except Exception as e:
            _proxy_logger.warning("[%s] Upstream unavailable for prompt listing: %s", self._backend_name, e)
            if self._cache_listings:
                cached = _load_cache(self._backend_name, "prompts", mcp.types.Prompt)
                if cached is not None:
                    _proxy_logger.info("[%s] Returning %d cached prompts", self._backend_name, len(cached))
                    return cached
            return []


class ResilientFastMCPProxy(FastMCPProxy):
    """FastMCPProxy that uses ResilientProxyProvider for graceful upstream failure handling."""

    def __init__(self, *, client_factory, backend_name: str = "upstream", cache_listings: bool = False, **kwargs):
        # Call grandparent (FastMCP) init, skipping FastMCPProxy which adds a plain ProxyProvider
        FastMCPServer.__init__(self, **kwargs)
        self.client_factory = client_factory
        provider = ResilientProxyProvider(client_factory, backend_name=backend_name, cache_listings=cache_listings)
        self.add_provider(provider)


import objc
from AppKit import (
    NSAttributedString, NSBezierPath, NSColor, NSFont, NSFontAttributeName,
    NSForegroundColorAttributeName, NSImage, NSMutableAttributedString,
    NSTextAttachment, NSTextAttachmentCell,
)
from Foundation import NSObject, NSTimer, NSRunLoop

load_dotenv()

# ── OAuth scope mappings ─────────────────────────────────────────────────────

OAUTH_SCOPES = {
    "microsoft": {
        "rag": ["User.Read"],
    },
    "google": {
        "rag": "openid email profile",
        "google_workspace": (
            "openid email profile"
            " https://www.googleapis.com/auth/spreadsheets"
            " https://www.googleapis.com/auth/documents"
            " https://www.googleapis.com/auth/presentations"
            " https://www.googleapis.com/auth/drive"
        ),
    },
}


def _scopes_for_app(app, backend):
    """Compute OAuth scopes for one specific backend (not a union)."""
    app_type = app["type"]
    if app_type == "microsoft":
        return list(OAUTH_SCOPES["microsoft"].get(backend, ["User.Read"]))
    else:  # google
        scope_str = OAUTH_SCOPES["google"].get(backend, "openid email profile")
        return scope_str


# ── Global config ────────────────────────────────────────────────────────────

REDIRECT_PORT = int(os.environ.get("REDIRECT_PORT", "53214"))
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}"
GOOGLE_MCP_PORT = int(os.environ.get("GOOGLE_MCP_PORT", "18766"))
JIRA_MCP_PORT = int(os.environ.get("JIRA_MCP_PORT", "18767"))
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
        super().__init__("Voitta", title="V")

        self._auth_lock = threading.Lock()
        self._auth = {}  # keyed by (app_id, backend)

        # Load config from apps.json (migrate from legacy if needed)
        self._config = self._load_or_migrate_config()

        # Proxy / path settings
        proxy = self._config.get("proxy", {})
        self.voitta_rag_url = proxy.get("rag_url", "https://rag.voitta.ai")
        self.edit_proxy_url = proxy.get("edit_proxy_url", f"http://localhost:{GOOGLE_MCP_PORT}")
        self.proxy_port = proxy.get("port", 18765)
        self.edit_mcp_env_path = EDIT_MCP_ENV_PATH
        self.jira_mcp_env_path = JIRA_MCP_ENV_PATH

        # Init auth state per (app, backend) pair — each gets independent auth
        for app in self._config.get("apps", []):
            for backend in app.get("use_for", []):
                self._auth[(app["id"], backend)] = {
                    "token": None,
                    "refresh_token": None,
                    "profile": None,
                    "refresh_timer": None,
                    "msal_app": None,
                }
            if app["type"] == "microsoft":
                self._rebuild_msal_for_app(app)

        # Active app per (backend, type) — determines which token is sent in headers.
        # {("rag", "google"): "app-uuid", ("google_workspace", "microsoft"): "app-uuid", ...}
        self._active_app = {}
        self._init_active_defaults()

        # Sync .env files and start subprocesses
        self._sync_edit_mcp_env()
        self._sync_jira_mcp_env()
        self._start_mcp_subprocesses()

        # Build sectioned menu
        self._menu_items = {}
        self._build_menu()

        self._update_menu_state()

        # Start unified FastMCP proxy server
        threading.Thread(target=self._run_fastmcp_proxy, daemon=True).start()

    # ── Config ────────────────────────────────────────────────────────────────

    def _load_or_migrate_config(self):
        """Load apps.json, migrating from legacy settings if needed."""
        if CONFIG_PATH.exists():
            return load_config()
        legacy = {}
        if SETTINGS_PATH.exists():
            try:
                legacy = json.loads(SETTINGS_PATH.read_text())
            except Exception:
                pass
        config = migrate_from_legacy(legacy)
        save_config(config)
        return config

    def _app_by_id(self, app_id):
        """Find an app config dict by its ID."""
        return next((a for a in self._config.get("apps", []) if a["id"] == app_id), None)

    def _init_active_defaults(self):
        """Set default active app per (backend, type) — first app of each type wins."""
        for backend in ("rag", "google_workspace"):
            for app in apps_for_backend(self._config, backend):
                key = (backend, app["type"])
                if key not in self._active_app:
                    self._active_app[key] = app["id"]

    def _set_active(self, backend, app_id):
        """Set an app as the active one for its (backend, type) pair."""
        app = self._app_by_id(app_id)
        if not app:
            return
        self._active_app[(backend, app["type"])] = app_id
        print(f"[voitta-auth] Active {backend}/{app['type']}: {app['name']}")
        self._update_menu_state()

    def _is_active(self, backend, app_id):
        """Check if an app is the active one for its (backend, type) pair."""
        app = self._app_by_id(app_id)
        if not app:
            return False
        return self._active_app.get((backend, app["type"])) == app_id

    # ── Menu ──────────────────────────────────────────────────────────────────

    def _build_menu(self):
        """Build sectioned menu from config.

        If a backend has multiple apps of the same type, they're grouped into
        a submenu.  Clicking a submenu item sets it as active (whose token is
        sent in headers) and triggers auth if not yet authenticated.
        """
        menu_list = []

        # MCP Proxy status
        proxy_item = rumps.MenuItem(f"MCP  http://127.0.0.1:{self.proxy_port}/mcp")
        proxy_item.set_callback(None)
        self._menu_items["proxy"] = proxy_item
        menu_list.append(proxy_item)
        menu_list.append(None)

        for backend, label in (("rag", "RAG (voitta.ai)"), ("google_workspace", "Google Workspace")):
            backend_apps = apps_for_backend(self._config, backend)
            # Microsoft apps cannot be used with Google Workspace
            if backend == "google_workspace":
                backend_apps = [a for a in backend_apps if a["type"] != "microsoft"]
            if not backend_apps:
                continue
            header = rumps.MenuItem(label)
            header.set_callback(None)
            menu_list.append(header)

            # Group apps by type
            by_type = {}
            for app in backend_apps:
                by_type.setdefault(app["type"], []).append(app)

            for app_type, apps_of_type in by_type.items():
                if len(apps_of_type) == 1:
                    # Single app of this type — flat menu item
                    app = apps_of_type[0]
                    item = rumps.MenuItem("", callback=self._make_app_toggle(app["id"], backend))
                    self._menu_items[f"{backend}:{app['id']}"] = item
                    menu_list.append(item)
                else:
                    # Multiple apps of same type — submenu
                    type_label = "Microsoft" if app_type == "microsoft" else "Google"
                    parent = rumps.MenuItem(type_label)
                    for app in apps_of_type:
                        sub_item = rumps.MenuItem(
                            "", callback=self._make_app_activate(backend, app["id"])
                        )
                        self._menu_items[f"{backend}:{app['id']}"] = sub_item
                        parent.add(sub_item)
                    menu_list.append(parent)
                    self._menu_items[f"{backend}:{app_type}:parent"] = parent

            menu_list.append(None)

        # Jira section
        header = rumps.MenuItem("Jira")
        header.set_callback(None)
        menu_list.append(header)
        jira_item = rumps.MenuItem("")
        jira_item.set_callback(None)
        self._menu_items["jira"] = jira_item
        menu_list.append(jira_item)
        menu_list.append(None)

        # Bottom bar
        menu_list.append(rumps.MenuItem("Settings", callback=self.show_settings))
        menu_list.append(rumps.MenuItem("Help", callback=self.show_help))

        self.menu = menu_list

    def _rebuild_menu(self):
        """Clear and rebuild the menu (e.g. after settings change)."""
        self._menu_items = {}
        self.menu.clear()
        self._build_menu()

    def _app_menu_title(self, app, backend, is_submenu=False):
        """Build menu item title for an app: ●/○  Name  email/Not connected.

        If is_submenu is True and this app is the active+connected one,
        a checkmark is prepended.
        """
        state = self._auth.get((app["id"], backend), {})
        connected = state.get("token") is not None
        dot = "\u25CF" if connected else "\u25CB"  # ● / ○
        profile = state.get("profile") or {}
        right = profile.get("email", "") if connected else "Not connected"
        name = app.get("name", app["type"].capitalize())
        prefix = ""
        if is_submenu and connected and self._is_active(backend, app["id"]):
            prefix = "\u2713 "  # ✓
        return f"{prefix}{dot}  {name:<30} {right}"

    def _jira_menu_title(self):
        """Build Jira menu item title."""
        jira = self._config.get("jira", {})
        if jira.get("server_url") and jira.get("email") and jira.get("api_token"):
            project = jira.get("project", "")
            email = jira.get("email", "")
            dot = "\u25CF"
            if project:
                return f"{dot}  Jira Cloud                  {project} ({email})"
            return f"{dot}  Jira Cloud                  {email}"
        return "\u25CB  Jira Cloud                  Not configured"

    def _build_title(self):
        """Build plain-text fallback title."""
        seen = []
        for app in self._config.get("apps", []):
            letter = "M" if app["type"] == "microsoft" else "G"
            if letter not in seen:
                seen.append(letter)
        if self._has_jira_credentials():
            seen.append("J")
        return " ".join(seen) if seen else "V"

    def _apply_attributed_title(self):
        """Set menu bar title with dimmed/bright letters based on auth state."""
        try:
            button = self._nsapp.nsstatusitem.button()
        except AttributeError:
            return
        is_dark = "Dark" in str(button.effectiveAppearance().name())
        base = 1.0 if is_dark else 0.0
        font = NSFont.menuBarFontOfSize_(0)
        title = NSMutableAttributedString.alloc().init()

        # App type letters (M, G) — bright if any app of that type is authenticated
        seen_types = []
        for app in self._config.get("apps", []):
            t = app["type"]
            if t not in seen_types:
                seen_types.append(t)

        for i, app_type in enumerate(seen_types):
            if i > 0:
                space = NSAttributedString.alloc().initWithString_attributes_(
                    " ", {NSFontAttributeName: font}
                )
                title.appendAttributedString_(space)
            letter = "M" if app_type == "microsoft" else "G"
            active = any(
                state.get("token") is not None
                for key, state in self._auth.items()
                if self._app_by_id(key[0]) and self._app_by_id(key[0])["type"] == app_type
            )
            alpha = 1.0 if active else 0.4
            color = NSColor.colorWithCalibratedWhite_alpha_(base, alpha)
            attrs = {NSForegroundColorAttributeName: color, NSFontAttributeName: font}
            char = NSAttributedString.alloc().initWithString_attributes_(letter, attrs)
            title.appendAttributedString_(char)

        # Jira letter
        if title.length() > 0:
            space = NSAttributedString.alloc().initWithString_attributes_(
                " ", {NSFontAttributeName: font}
            )
            title.appendAttributedString_(space)
        jira_active = self._has_jira_credentials()
        alpha = 1.0 if jira_active else 0.4
        color = NSColor.colorWithCalibratedWhite_alpha_(base, alpha)
        attrs = {NSForegroundColorAttributeName: color, NSFontAttributeName: font}
        j_char = NSAttributedString.alloc().initWithString_attributes_("J", attrs)
        title.appendAttributedString_(j_char)

        button.setAttributedTitle_(title)

    @rumps.timer(0.1)
    def _startup_title(self, timer):
        """Apply attributed title once the status bar is ready."""
        self._apply_attributed_title()
        timer.stop()

    def _has_jira_credentials(self):
        """Return True if Jira is fully configured."""
        jira = self._config.get("jira", {})
        return bool(jira.get("server_url") and jira.get("email")
                     and jira.get("api_token") and jira.get("project"))

    def _update_menu_state(self):
        self.title = self._build_title()
        self._apply_attributed_title()

        # Update app menu items
        for backend in ("rag", "google_workspace"):
            backend_apps = apps_for_backend(self._config, backend)
            # Group by type to detect submenus
            by_type = {}
            for app in backend_apps:
                by_type.setdefault(app["type"], []).append(app)

            for app_type, apps_of_type in by_type.items():
                is_submenu = len(apps_of_type) > 1
                for app in apps_of_type:
                    key = f"{backend}:{app['id']}"
                    if key in self._menu_items:
                        self._menu_items[key].title = self._app_menu_title(
                            app, backend, is_submenu=is_submenu
                        )

                # Update parent submenu title with active app's status
                if is_submenu:
                    parent_key = f"{backend}:{app_type}:parent"
                    if parent_key in self._menu_items:
                        active_id = self._active_app.get((backend, app_type))
                        type_label = "Microsoft" if app_type == "microsoft" else "Google"
                        if active_id:
                            state = self._auth.get((active_id, backend), {})
                            profile = state.get("profile") or {}
                            email = profile.get("email", "")
                            if email:
                                type_label = f"{type_label} ({email})"
                        self._menu_items[parent_key].title = type_label

        # Jira status
        if "jira" in self._menu_items:
            self._menu_items["jira"].title = self._jira_menu_title()

    def _make_app_toggle(self, app_id, backend):
        """Create a callback that toggles auth for an app on a specific backend."""
        def callback(_):
            state = self._auth.get((app_id, backend), {})
            if state.get("token"):
                self._deauth_app(app_id, backend)
            else:
                threading.Thread(
                    target=self._do_auth, args=(app_id, backend), daemon=True
                ).start()
        return callback

    def _make_app_activate(self, backend, app_id):
        """Create a callback that sets an app as active + triggers auth if needed."""
        def callback(_):
            self._set_active(backend, app_id)
            state = self._auth.get((app_id, backend), {})
            if not state.get("token"):
                threading.Thread(
                    target=self._do_auth, args=(app_id, backend), daemon=True
                ).start()
        return callback

    # ── MSAL (Microsoft) ─────────────────────────────────────────────────────

    def _rebuild_msal_for_app(self, app):
        """Build or rebuild MSAL app for a Microsoft app config (per-backend)."""
        tenant_id = app.get("tenant_id", "")
        client_id = app.get("client_id", "")
        for backend in app.get("use_for", []):
            state = self._auth.get((app["id"], backend))
            if not state:
                continue
            if tenant_id and client_id:
                state["msal_app"] = msal.PublicClientApplication(
                    client_id,
                    authority=f"https://login.microsoftonline.com/{tenant_id}",
                )
            else:
                state["msal_app"] = None

    # ── Edit MCP .env sync ────────────────────────────────────────────────────

    def _sync_edit_mcp_env(self):
        """Write Google Workspace credentials to the workspace MCP server's .env file."""
        env_path = self.edit_mcp_env_path
        if not env_path:
            return

        # Find first Google app assigned to google_workspace
        gw_google = [a for a in self._config.get("apps", [])
                      if a["type"] == "google" and "google_workspace" in a.get("use_for", [])]
        if not gw_google:
            print("[voitta-auth] Skipping edit MCP .env sync — no Google Workspace app")
            return

        app = gw_google[0]
        client_id = app.get("client_id", "")
        client_secret = app.get("client_secret", "")
        if not client_id or not client_secret:
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

        jira = self._config.get("jira", {})
        server_url = jira.get("server_url", "")
        email = jira.get("email", "")
        token = jira.get("api_token", "")

        if not server_url or not email or not token:
            print("[voitta-auth] Skipping Jira MCP .env sync — missing credentials")
            return

        project = jira.get("project", "")

        lines = [
            "# Managed by voitta-auth — do not edit manually",
            f"JIRA_URL={server_url}",
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

    def _do_auth(self, app_id, backend):
        """Start OAuth flow for an app on a specific backend."""
        app = self._app_by_id(app_id)
        if not app:
            return
        if not self._auth_lock.acquire(blocking=False):
            _notify("Voitta Auth", "Busy", "Another authentication is in progress.")
            return
        try:
            print(f"[voitta-auth] Starting {app['name']} ({backend}) OAuth2 flow...")
            if app["type"] == "microsoft":
                self._do_auth_microsoft(app, backend)
            elif app["type"] == "google":
                self._do_auth_google(app, backend)
        except Exception as e:
            print(f"[voitta-auth] {app['name']} EXCEPTION: {e}")
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

    def _do_auth_microsoft(self, app, backend):
        state = self._auth[(app["id"], backend)]
        msal_app = state["msal_app"]
        if not msal_app:
            _notify("Voitta Auth", app["name"], "Configure Tenant ID and Client ID in Settings first.")
            return

        scopes = _scopes_for_app(app, backend)
        auth_url = msal_app.get_authorization_request_url(scopes, redirect_uri=REDIRECT_URI)
        webbrowser.open(auth_url)
        code, error = self._wait_for_callback()

        if not code:
            _notify("Voitta Auth", app["name"], error or "No authorization code received.")
            return

        print(f"[voitta-auth] Got {app['name']} ({backend}) auth code, exchanging for token...")
        result = msal_app.acquire_token_by_authorization_code(
            code, scopes=scopes, redirect_uri=REDIRECT_URI
        )

        if "access_token" in result:
            state["token"] = result["access_token"]
            self._fetch_profile_microsoft(app["id"], backend)
            self._schedule_refresh(app["id"], backend, result.get("expires_in", 3600))
            name = state["profile"].get("name", "Unknown") if state["profile"] else "Unknown"
            print(f"[voitta-auth] {app['name']} ({backend}) authenticated as {name}")
            self._update_menu_state()
            _notify("Voitta Auth", app["name"], f"Welcome, {name}!")
        else:
            error = result.get("error_description", result.get("error", "Unknown error"))
            print(f"[voitta-auth] {app['name']} token exchange failed: {error}")
            _notify("Voitta Auth", app["name"], str(error))

    def _fetch_profile_microsoft(self, app_id, backend):
        state = self._auth[(app_id, backend)]
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

    def _do_auth_google(self, app, backend):
        client_id = app.get("client_id", "")
        client_secret = app.get("client_secret", "")
        if not client_id or not client_secret:
            _notify("Voitta Auth", app["name"], "Configure Client ID and Client Secret in Settings first.")
            return

        scopes = _scopes_for_app(app, backend)
        verifier, challenge = _pkce_pair()
        params = {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": scopes,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
        }
        auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
        webbrowser.open(auth_url)
        code, error = self._wait_for_callback()

        if not code:
            _notify("Voitta Auth", app["name"], error or "No authorization code received.")
            return

        print(f"[voitta-auth] Got {app['name']} ({backend}) auth code, exchanging for token...")
        resp = requests.post("https://oauth2.googleapis.com/token", data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": REDIRECT_URI,
        }, timeout=10)

        if not resp.ok:
            _notify("Voitta Auth", app["name"], f"Token exchange failed: {resp.text[:200]}")
            return

        data = resp.json()
        state = self._auth[(app["id"], backend)]
        state["token"] = data["access_token"]
        state["refresh_token"] = data.get("refresh_token")
        self._fetch_profile_google(app["id"], backend)
        self._schedule_refresh(app["id"], backend, data.get("expires_in", 3600))
        name = state["profile"].get("name", "Unknown") if state["profile"] else "Unknown"
        print(f"[voitta-auth] {app['name']} ({backend}) authenticated as {name}")
        self._update_menu_state()
        _notify("Voitta Auth", app["name"], f"Welcome, {name}!")

    def _fetch_profile_google(self, app_id, backend):
        state = self._auth[(app_id, backend)]
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

    def _schedule_refresh(self, app_id, backend, expires_in):
        state = self._auth.get((app_id, backend))
        if not state:
            return
        if state["refresh_timer"]:
            state["refresh_timer"].cancel()
        refresh_in = max(expires_in - 300, 60)

        app = self._app_by_id(app_id)
        if not app:
            return

        if app["type"] == "microsoft":
            timer = threading.Timer(refresh_in, self._do_refresh_microsoft, args=(app_id, backend))
        elif app["type"] == "google":
            timer = threading.Timer(refresh_in, self._do_refresh_google, args=(app_id, backend))
        else:
            return

        timer.daemon = True
        timer.start()
        state["refresh_timer"] = timer
        print(f"[voitta-auth] {app['name']} ({backend}) token refresh scheduled in {refresh_in}s")

    def _do_refresh_microsoft(self, app_id, backend):
        state = self._auth.get((app_id, backend))
        if not state:
            return
        msal_app = state["msal_app"]
        if not msal_app:
            return
        app = self._app_by_id(app_id)
        if not app:
            return
        accounts = msal_app.get_accounts()
        if not accounts:
            return
        scopes = _scopes_for_app(app, backend)
        result = msal_app.acquire_token_silent(scopes, account=accounts[0], force_refresh=True)
        if result and "access_token" in result:
            state["token"] = result["access_token"]
            self._schedule_refresh(app_id, backend, result.get("expires_in", 3600))
            print(f"[voitta-auth] {app['name']} ({backend}) token refreshed silently")
        else:
            print(f"[voitta-auth] {app['name']} ({backend}) silent refresh failed")
            state["token"] = None
            state["profile"] = None
            self._update_menu_state()

    def _do_refresh_google(self, app_id, backend):
        state = self._auth.get((app_id, backend))
        if not state or not state["refresh_token"]:
            return
        app = self._app_by_id(app_id)
        if not app:
            return
        client_id = app.get("client_id", "")
        client_secret = app.get("client_secret", "")
        try:
            resp = requests.post("https://oauth2.googleapis.com/token", data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": state["refresh_token"],
            }, timeout=10)
            if resp.ok:
                data = resp.json()
                state["token"] = data["access_token"]
                if "refresh_token" in data:
                    state["refresh_token"] = data["refresh_token"]
                self._schedule_refresh(app_id, backend, data.get("expires_in", 3600))
                print(f"[voitta-auth] {app['name']} ({backend}) token refreshed silently")
            else:
                print(f"[voitta-auth] {app['name']} ({backend}) refresh failed")
                state["token"] = None
                state["refresh_token"] = None
                state["profile"] = None
                self._update_menu_state()
        except Exception as e:
            print(f"[voitta-auth] {app['name']} ({backend}) refresh error: {e}")
            state["token"] = None
            state["refresh_token"] = None
            state["profile"] = None
            self._update_menu_state()

    # ── Deauthentication ─────────────────────────────────────────────────────

    def _deauth_app(self, app_id, backend=None):
        """Sign out an app. If backend is given, only that backend; otherwise all backends."""
        app = self._app_by_id(app_id)
        name = app["name"] if app else app_id
        backends = [backend] if backend else [b for b in (app or {}).get("use_for", [])]
        for b in backends:
            state = self._auth.get((app_id, b))
            if not state:
                continue
            if state["refresh_timer"]:
                state["refresh_timer"].cancel()
                state["refresh_timer"] = None
            if state["msal_app"]:
                for account in state["msal_app"].get_accounts():
                    state["msal_app"].remove_account(account)
            state["token"] = None
            state["refresh_token"] = None
            state["profile"] = None
        label = f"{name} ({backend})" if backend else name
        print(f"[voitta-auth] {label} signed out")
        _notify("Voitta Auth", name, "Signed out.")
        self._update_menu_state()

    # ── FastMCP Proxy ─────────────────────────────────────────────────────────

    def _make_rag_client_factory(self):
        """Return a factory that creates a ProxyClient with current RAG auth headers.

        For each provider type (Microsoft, Google), only the *active* app's token
        is sent.  This avoids header collisions when multiple apps of the same type
        are configured.
        """
        app_ref = self
        def factory():
            headers = {}
            for app_type in ("microsoft", "google"):
                active_id = app_ref._active_app.get(("rag", app_type))
                if not active_id:
                    continue
                state = app_ref._auth.get((active_id, "rag"), {})
                if not state.get("token"):
                    continue
                suffix = app_type.capitalize()  # "Microsoft" or "Google"
                headers[f"X-Auth-Token-{suffix}"] = f"Bearer {state['token']}"
                profile = state.get("profile") or {}
                if profile.get("email"):
                    headers[f"X-Auth-Email-{suffix}"] = profile["email"]
                if profile.get("name"):
                    headers[f"X-Auth-Name-{suffix}"] = profile["name"]
            url = f"{app_ref.voitta_rag_url.rstrip('/')}/mcp/mcp"
            print(f"[voitta-auth] RAG factory: url={url}, {len(headers)} headers")
            transport = StreamableHttpTransport(url=url, headers=headers)
            return ProxyClient(transport)
        return factory

    def _make_google_client_factory(self):
        """Return a factory that creates a ProxyClient with current Google Workspace Bearer token."""
        app_ref = self
        def factory():
            headers = {}
            active_id = app_ref._active_app.get(("google_workspace", "google"))
            if active_id:
                state = app_ref._auth.get((active_id, "google_workspace"), {})
                if state.get("token"):
                    headers["Authorization"] = f"Bearer {state['token']}"
                    profile = state.get("profile") or {}
                    if profile.get("email"):
                        headers["X-Auth-Email"] = profile["email"]
                    if profile.get("name"):
                        headers["X-Auth-Name"] = profile["name"]
            url = f"{app_ref.edit_proxy_url.rstrip('/')}/mcp"
            print(f"[voitta-auth] Google factory: url={url}, headers={list(headers.keys())}")
            transport = StreamableHttpTransport(url=url, headers=headers)
            return ProxyClient(transport)
        return factory

    def _run_fastmcp_proxy(self):
        """Run unified FastMCP proxy server mounting all backends."""
        main_server = FastMCPServer(
            "voitta-auth",
            instructions=(
                "You are connected through Voitta Auth, a unified MCP proxy. "
                "All tool names are prefixed by backend:\n"
                "  • voitta_rag_*   — RAG search, memory, file retrieval\n"
                "  • google_workspace_* — Google Workspace (Gmail, Drive, Sheets, Docs, Calendar)\n"
                "  • jira_*         — Jira issues, sprints, boards\n"
                "If a google_workspace_* tool fails with an auth error, "
                "ask the user to log in via the Voitta Auth menu bar icon."
            ),
        )

        # RAG proxy with dynamic per-provider auth headers
        rag_proxy = ResilientFastMCPProxy(
            client_factory=self._make_rag_client_factory(),
            name="voitta-rag",
            backend_name="RAG",
        )
        main_server.mount(rag_proxy, prefix="voitta_rag")

        # Google Workspace proxy with dynamic Bearer token
        google_proxy = ResilientFastMCPProxy(
            client_factory=self._make_google_client_factory(),
            name="google-workspace",
            backend_name="Google Workspace",
            cache_listings=True,
        )
        main_server.mount(google_proxy, prefix="google_workspace")

        # Jira proxy (credentials already in subprocess .env)
        jira_proxy = FastMCPServer.as_proxy(
            f"http://localhost:{JIRA_MCP_PORT}/mcp",
            name="jira",
        )
        main_server.mount(jira_proxy, prefix="jira")

        print(f"[voitta-auth] FastMCP proxy on http://127.0.0.1:{self.proxy_port}/mcp")
        print(f"[voitta-auth]   RAG → {self.voitta_rag_url}")
        print(f"[voitta-auth]   Google → {self.edit_proxy_url}")
        print(f"[voitta-auth]   Jira → http://localhost:{JIRA_MCP_PORT}/mcp")
        main_server.run(transport="streamable-http", host="127.0.0.1", port=self.proxy_port)

    # ── MCP subprocess management ────────────────────────────────────────────

    def _start_mcp_subprocesses(self):
        """Launch google_workspace_mcp and mcp-atlassian as background processes."""
        self._subprocesses = []

        # Google Workspace MCP
        if Path(GOOGLE_MCP_DIR).is_dir():
            try:
                proc = subprocess.Popen(
                    ["uv", "run", "main.py", "--transport", "streamable-http", "--port", str(GOOGLE_MCP_PORT)],
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

    # ── Settings (WKWebView) ─────────────────────────────────────────────────

    def show_settings(self, _):
        """Open settings in a native WKWebView window."""
        from AppKit import NSApp, NSBackingStoreBuffered, NSFloatingWindowLevel, NSWindow
        from Foundation import NSMakeRect
        from WebKit import WKWebView

        mask = 1 | 2 | 8  # titled | closable | resizable
        frame = NSMakeRect(200, 200, 540, 650)
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, mask, NSBackingStoreBuffered, False
        )
        window.setTitle_("Voitta Auth \u2014 Settings")
        window.center()

        webview = WKWebView.alloc().initWithFrame_(window.contentView().bounds())
        webview.setAutoresizingMask_(18)  # width + height flexible
        window.contentView().addSubview_(webview)

        # Embed config directly into HTML as inline script
        html_path = Path(__file__).parent / "ui" / "settings.html"
        html_content = html_path.read_text(encoding="utf-8")
        config_json = json.dumps(self._config)
        html_content = html_content.replace(
            "/*INJECT_CONFIG*/",
            f"var _initialConfig = {config_json};",
        )
        webview.loadHTMLString_baseURL_(html_content, None)

        # KVO observer on webview.title — JS sets document.title to signal save/cancel
        observer = _SettingsTitleObserver.alloc().initWithApp_window_(self, window)
        webview.addObserver_forKeyPath_options_context_(observer, "title", 1, None)

        # Store strong references to prevent GC / segfaults
        self._settings_refs = (window, webview, observer)

        window.setLevel_(NSFloatingWindowLevel)
        NSApp.setActivationPolicy_(0)
        NSApp.activateIgnoringOtherApps_(True)
        window.makeKeyAndOrderFront_(None)

        # Delayed focus grab (same pattern as _show_modal)
        trigger = _FocusTrigger.alloc().init()
        trigger.setWindow_field_(window, None)
        timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            0.1, trigger, "focus:", None, False
        )
        NSRunLoop.mainRunLoop().addTimer_forMode_(timer, "NSDefaultRunLoopMode")

    def _apply_settings(self, new_config):
        """Apply a new config dict from the settings page."""
        old_keys = set(self._auth.keys())

        self._config = new_config
        save_config(new_config)

        # Build new set of (app_id, backend) keys
        new_keys = set()
        for app in new_config.get("apps", []):
            for backend in app.get("use_for", []):
                new_keys.add((app["id"], backend))

        # Init auth state for new (app, backend) pairs
        for app in new_config.get("apps", []):
            for backend in app.get("use_for", []):
                if (app["id"], backend) not in self._auth:
                    self._auth[(app["id"], backend)] = {
                        "token": None, "refresh_token": None,
                        "profile": None, "refresh_timer": None,
                        "msal_app": None,
                    }
            if app["type"] == "microsoft":
                self._rebuild_msal_for_app(app)

        # Remove auth for deleted (app, backend) pairs
        for key in old_keys - new_keys:
            app_id, backend = key
            self._deauth_app(app_id, backend)
            self._auth.pop(key, None)

        # Update proxy settings
        proxy = new_config.get("proxy", {})
        self.voitta_rag_url = proxy.get("rag_url", "https://rag.voitta.ai")
        self.edit_proxy_url = proxy.get("edit_proxy_url", f"http://localhost:{GOOGLE_MCP_PORT}")

        # Re-init active app defaults (new apps get a default active slot)
        self._init_active_defaults()

        # Sync and rebuild
        self._sync_edit_mcp_env()
        self._sync_jira_mcp_env()
        self._rebuild_menu()
        self._update_menu_state()
        print("[voitta-auth] Settings saved and applied")

    # ── Help ─────────────────────────────────────────────────────────────────

    def show_help(self, _):
        from AppKit import NSAlert
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Voitta Auth Help")
        alert.setInformativeText_(
            "Voitta Auth sits in your menu bar.\n\n"
            "Bright letters = authenticated, dimmed = not.\n"
            "Click an app to connect/disconnect.\n\n"
            "Manage OAuth applications and Jira credentials\n"
            "via Settings.\n\n"
            f"MCP proxy: http://127.0.0.1:{self.proxy_port}/mcp\n"
            f"  RAG \u2192 {self.voitta_rag_url}\n"
            f"  Google \u2192 {self.edit_proxy_url}\n"
            f"  Jira \u2192 http://127.0.0.1:{JIRA_MCP_PORT}/mcp"
        )
        alert.addButtonWithTitle_("OK")
        _show_modal(alert)


# ── WKWebView settings bridge (KVO on document.title) ────────────────────────

class _SettingsTitleObserver(NSObject):
    """KVO observer on WKWebView.title.

    JS signals save/cancel by setting document.title:
      - "VOITTA_SAVE:<json>"  → apply settings and close
      - "VOITTA_CANCEL"       → close without saving
    """

    def initWithApp_window_(self, app_ref, window):
        self = objc.super(_SettingsTitleObserver, self).init()
        if self is not None:
            self._app = app_ref
            self._window = window
            self._handled = False
        return self

    def observeValueForKeyPath_ofObject_change_context_(
        self, keyPath, obj, change, context
    ):
        if self._handled:
            return
        title = obj.title()
        if not title:
            return

        if title == "VOITTA_SAVE":
            self._handled = True
            try:
                obj.removeObserver_forKeyPath_(self, "title")
            except Exception:
                pass
            # Read full config data via evaluateJavaScript (title truncates)
            obj.evaluateJavaScript_completionHandler_(
                "JSON.stringify(collectAll())", self.onSaveData_error_
            )
            return

        elif title == "VOITTA_CANCEL":
            self._handled = True
            try:
                obj.removeObserver_forKeyPath_(self, "title")
            except Exception:
                pass
            self._deferClose()

    def onSaveData_error_(self, result, error):
        """Completion handler for evaluateJavaScript — receives the config JSON."""
        if error:
            print(f"[voitta-auth] Settings JS error: {error}")
        elif result:
            try:
                data = json.loads(result)
                self._app._apply_settings(data)
            except Exception as e:
                print(f"[voitta-auth] Settings save error: {e}")
        self._deferClose()

    def _deferClose(self):
        """Hide window on next run loop tick. Don't close/dealloc — WKWebView segfaults."""
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.0, self, "doClose:", None, False
        )

    def doClose_(self, timer):
        from AppKit import NSApp
        if self._window:
            self._window.orderOut_(None)  # hide, don't close (avoids WKWebView dealloc crash)
        NSApp.setActivationPolicy_(1)  # back to accessory (menu bar only)


if __name__ == "__main__":
    VoittaAuthApp().run()
