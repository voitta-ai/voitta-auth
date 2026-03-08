"""Voitta Auth — Persistent configuration (apps.json)."""

import json
import os
import uuid
from pathlib import Path

CONFIG_DIR = Path.home() / ".voitta_auth"
CONFIG_PATH = CONFIG_DIR / "apps.json"

DEFAULT_CONFIG = {
    "apps": [],
    "jira": {
        "server_url": "",
        "email": "",
        "api_token": "",
        "project": "",
    },
    "proxy": {
        "port": 18765,
        "edit_proxy_url": "http://localhost:8000",
        "rag_url": "https://rag.voitta.ai",
    },
}


def load_config() -> dict:
    """Load config from disk, returning defaults if missing or corrupt."""
    if not CONFIG_PATH.exists():
        return json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    try:
        data = json.loads(CONFIG_PATH.read_text())
        # Ensure required top-level keys exist
        for key in DEFAULT_CONFIG:
            if key not in data:
                data[key] = json.loads(json.dumps(DEFAULT_CONFIG[key]))
        return data
    except Exception:
        return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(config: dict) -> None:
    """Write config to disk."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2))


def migrate_from_legacy(settings: dict, env_defaults: dict | None = None) -> dict:
    """Build apps.json config from legacy ~/.voitta_auth_settings.json values.

    Called once on first run when apps.json doesn't exist.
    """
    config = json.loads(json.dumps(DEFAULT_CONFIG))

    # --- RAG identity providers ---

    # Microsoft (RAG)
    ms_tenant = settings.get("ms_tenant_id") or os.environ.get("AZURE_TENANT_ID", "")
    ms_client = settings.get("ms_client_id") or os.environ.get("AZURE_CLIENT_ID", "")
    if ms_tenant or ms_client:
        config["apps"].append({
            "id": str(uuid.uuid4()),
            "name": "Microsoft",
            "type": "microsoft",
            "tenant_id": ms_tenant,
            "client_id": ms_client,
            "use_for": ["rag"],
        })

    # Google (RAG)
    g_client = settings.get("google_client_id") or os.environ.get("GOOGLE_CLIENT_ID", "")
    g_secret = settings.get("google_client_secret") or os.environ.get("GOOGLE_CLIENT_SECRET", "")
    if g_client or g_secret:
        config["apps"].append({
            "id": str(uuid.uuid4()),
            "name": "Google",
            "type": "google",
            "client_id": g_client,
            "client_secret": g_secret,
            "use_for": ["rag"],
        })

    # --- Google Workspace providers ---

    # Google Edit
    ge_client = settings.get("google_edit_client_id") or g_client
    ge_secret = settings.get("google_edit_client_secret") or g_secret
    if ge_client or ge_secret:
        # Reuse existing Google app if credentials match
        existing = next(
            (a for a in config["apps"]
             if a["type"] == "google" and a["client_id"] == ge_client and a.get("client_secret") == ge_secret),
            None,
        )
        if existing:
            if "google_workspace" not in existing["use_for"]:
                existing["use_for"].append("google_workspace")
        else:
            config["apps"].append({
                "id": str(uuid.uuid4()),
                "name": "Google Edit",
                "type": "google",
                "client_id": ge_client,
                "client_secret": ge_secret,
                "use_for": ["google_workspace"],
            })

    # Microsoft Edit
    me_tenant = settings.get("ms_edit_tenant_id") or ms_tenant
    me_client = settings.get("ms_edit_client_id") or ms_client
    if me_tenant or me_client:
        existing = next(
            (a for a in config["apps"]
             if a["type"] == "microsoft" and a["client_id"] == me_client and a.get("tenant_id") == me_tenant),
            None,
        )
        if existing:
            if "google_workspace" not in existing["use_for"]:
                existing["use_for"].append("google_workspace")
        else:
            config["apps"].append({
                "id": str(uuid.uuid4()),
                "name": "Microsoft Edit",
                "type": "microsoft",
                "tenant_id": me_tenant,
                "client_id": me_client,
                "use_for": ["google_workspace"],
            })

    # --- Jira ---
    jira_url = settings.get("jira_url", "") or os.environ.get("JIRA_URL", "")
    config["jira"] = {
        "server_url": settings.get("jira_server_url", jira_url),
        "email": settings.get("jira_email", "") or os.environ.get("JIRA_EMAIL", ""),
        "api_token": settings.get("jira_api_token", "") or os.environ.get("JIRA_API_TOKEN", ""),
        "project": settings.get("jira_project", "") or os.environ.get("JIRA_PROJECT", ""),
    }

    # --- Proxy ---
    config["proxy"] = {
        "port": int(settings.get("proxy_port", os.environ.get("PROXY_PORT", "18765"))),
        "edit_proxy_url": settings.get("edit_proxy_url", os.environ.get("EDIT_PROXY_URL", "http://localhost:8000")),
        "rag_url": settings.get("voitta_rag_url", os.environ.get("VOITTA_RAG_URL", "https://rag.voitta.ai")),
    }

    return config


def apps_for_backend(config: dict, backend: str) -> list[dict]:
    """Return apps assigned to a given backend, in order."""
    return [a for a in config.get("apps", []) if backend in a.get("use_for", [])]
