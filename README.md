# Voitta Auth

A macOS menu bar application that authenticates users via Microsoft Entra ID (Azure AD) and injects auth credentials into [Claude Code](https://claude.com/claude-code)'s MCP configuration, enabling authenticated access to [voitta-rag](https://github.com/voitta-ai/voitta-rag).

## How It Works

1. Sits in the macOS menu bar (`V ○` = signed out, `V ●` = signed in)
2. On **Authenticate**, opens Microsoft login in the browser and captures the OAuth2 callback on a local port
3. Exchanges the authorization code for an access token and fetches the user profile from Microsoft Graph
4. Writes `X-Auth-Token`, `X-Auth-Email`, and `X-Auth-Name` headers into `~/.claude.json` under `mcpServers.voitta-rag.headers`
5. On **Deauthenticate**, clears the token and removes the headers

## Prerequisites

- macOS (uses [rumps](https://github.com/jaredks/rumps) for the menu bar)
- Python 3.11+
- An Azure AD / Entra ID app registration with a client secret and `http://localhost:<port>` redirect URI

## Quick Start

```bash
# Clone
git clone git@github.com:voitta-ai/voitta-auth.git
cd voitta-auth

# Configure
cp .env.sample .env
# Edit .env with your Azure AD credentials

# Install
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run
python app.py
```

## Configuration

All configuration is via `.env` (see `.env.sample`):

| Variable | Description |
|----------|-------------|
| `AZURE_TENANT_ID` | Azure AD tenant (directory) ID |
| `AZURE_CLIENT_ID` | Application (client) ID |
| `AZURE_CLIENT_SECRET` | Client secret value |
| `REDIRECT_PORT` | Local port for OAuth callback (default: `53214`) |

## License

This project is licensed under the [GNU Affero General Public License v3.0](LICENSE).
