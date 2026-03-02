# Voitta Auth

A macOS menu bar application that authenticates users via Microsoft Entra ID (Azure AD) and runs a local HTTP proxy that injects auth headers into requests to [voitta-rag](https://github.com/voitta-ai/voitta-rag). Designed for use with [Claude Code](https://claude.com/claude-code) MCP servers.

## How It Works

1. Sits in the macOS menu bar (`V ○` = signed out, `V ●` = signed in)
2. On **Authenticate**, opens Microsoft login in the browser and captures the OAuth2 callback on a local port
3. Exchanges the authorization code for an access token and fetches the user profile from Microsoft Graph
4. Runs a local HTTP proxy (default `http://127.0.0.1:18765`) that forwards requests to voitta-rag, injecting `X-Auth-Token`, `X-Auth-Email`, and `X-Auth-Name` headers
5. Tokens are held in memory and refreshed automatically while the app is running
6. On **Deauthenticate**, clears the token and MSAL session

## Prerequisites

- macOS (uses [rumps](https://github.com/jaredks/rumps) for the menu bar)
- Python 3.11+
- An Azure AD / Entra ID app registration (public client — no secret required) with `http://localhost:<port>` redirect URI

## Azure App Registration

Before using voitta-auth you need an app registration in Microsoft Entra ID (Azure AD). This is a one-time setup performed by a tenant admin.

### 1. Create the app registration

1. Go to the [Azure Portal](https://portal.azure.com) > **Microsoft Entra ID** > **App registrations** > **New registration**
2. Fill in:
   - **Name**: `voitta-auth` (or any name you prefer)
   - **Supported account types**: *Accounts in this organizational directory only* (single tenant)
   - **Redirect URI**: select **Public client/native (mobile & desktop)** and enter `http://localhost:53214` (must match `REDIRECT_PORT`)
3. Click **Register**
4. On the app's **Overview** page, copy:
   - **Application (client) ID** → this is your `AZURE_CLIENT_ID`
   - **Directory (tenant) ID** → this is your `AZURE_TENANT_ID`

### 2. Configure as a public client

Since voitta-auth uses the authorization code flow without a client secret, it must be configured as a public client:

1. Go to **Authentication** in the left sidebar
2. Under **Advanced settings**, set **Allow public client flows** to **Yes**
3. Click **Save**

> No client secret is needed. The app uses MSAL's `PublicClientApplication` with PKCE, which is the recommended flow for desktop apps.

### 3. Configure API permissions

The app needs minimal permissions to read the signed-in user's profile:

1. Go to **API permissions** > **Add a permission** > **Microsoft Graph** > **Delegated permissions**
2. Search for and add **User.Read** (this is usually added by default)
3. If your organization requires it, click **Grant admin consent** for the tenant

### 4. (Optional) Restrict who can sign in

By default, any user in your tenant can authenticate. To restrict access:

1. Go to **Enterprise applications** in the Entra ID portal (not App registrations)
2. Find your app by name and open it
3. Under **Properties**, set **Assignment required?** to **Yes**
4. Go to **Users and groups** > **Add user/group** and add the specific users or groups allowed to sign in

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
