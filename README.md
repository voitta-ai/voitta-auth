# Voitta Auth

A macOS menu bar application that authenticates users via Microsoft Entra ID (Azure AD) and runs a local HTTP proxy that injects auth headers into requests to [voitta-rag](https://github.com/voitta-ai/voitta-rag). Designed for use with [Claude Code](https://claude.com/claude-code) MCP servers.

## How It Works

1. Sits in the macOS menu bar (`V ○` = signed out, `V ●` = signed in)
2. On **Authenticate**, opens Microsoft login in the browser and captures the OAuth2 callback on a local port
3. Exchanges the authorization code for an access token and fetches the user profile from Microsoft Graph
4. Runs a local HTTP proxy (default `http://127.0.0.1:18765`) that forwards requests to voitta-rag, injecting `X-Auth-Token`, `X-Auth-Email`, and `X-Auth-Name` headers
5. Tokens are cached in the macOS Keychain and refreshed automatically — sessions survive app restarts
6. On **Deauthenticate**, clears the token, keychain cache, and MSAL session

## Prerequisites

- macOS (uses [rumps](https://github.com/jaredks/rumps) for the menu bar)
- Python 3.11+
- An Azure AD / Entra ID app registration (public client — no secret required) with `http://localhost:<port>` redirect URI

## Quick Start

```bash
# Clone
git clone git@github.com:voitta-ai/voitta-auth.git
cd voitta-auth

# Configure
cp .env.sample .env
# Edit .env with your Azure AD tenant and client IDs

# Install
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run
python app.py
```

## Configuration

Initial values come from `.env` (see `.env.sample`). After first launch, use the **Settings** menu to update values — they are persisted to `~/.voitta_auth_settings.json` and take precedence over `.env`.

| Variable | Description |
|----------|-------------|
| `AZURE_TENANT_ID` | Azure AD tenant (directory) ID |
| `AZURE_CLIENT_ID` | Application (client) ID |
| `REDIRECT_PORT` | Local port for OAuth callback (default: `53214`) |
| `PROXY_PORT` | Local port for the auth proxy (default: `18765`) |
| `VOITTA_RAG_URL` | Upstream voitta-rag URL (default: `https://rag.voitta.ai`) |

### Claude Code MCP Setup

Point your MCP server config at the local proxy instead of the upstream URL:

```json
{
  "mcpServers": {
    "voitta-rag": {
      "url": "http://127.0.0.1:18765/mcp"
    }
  }
}
```

## License

This project is licensed under the [GNU Affero General Public License v3.0](LICENSE).
