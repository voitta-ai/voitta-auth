# Voitta Auth

A macOS menu bar application that authenticates users via multiple identity providers (Microsoft, Google, Okta) and runs a local HTTP proxy that injects auth headers into requests to [voitta-rag](https://github.com/voitta-ai/voitta-rag). Designed for use with [Claude Code](https://claude.com/claude-code) MCP servers.

## How It Works

1. Sits in the macOS menu bar showing `M G O` — each letter represents a provider (Microsoft, Google, Okta). Bright when authenticated, dimmed when not. Adapts to the system light/dark theme
2. Each provider can be activated/deactivated independently
3. On activation, opens the provider's login page in the browser and captures the OAuth2 callback
4. Runs a local HTTP proxy (default `http://127.0.0.1:18765`) that forwards requests to voitta-rag, injecting auth headers for **all** authenticated providers simultaneously
5. Tokens are held in memory and refreshed automatically while the app is running

## Prerequisites

- macOS (uses [rumps](https://github.com/jaredks/rumps) for the menu bar)
- Python 3.11+
- At least one identity provider configured (see setup guides below)

## Provider Setup

You only need to configure the providers you plan to use.

### Microsoft (Entra ID / Azure AD)

One-time setup by a tenant admin:

1. Go to [Azure Portal](https://portal.azure.com) > **Microsoft Entra ID** > **App registrations** > **New registration**
2. Fill in:
   - **Name**: `voitta-auth`
   - **Supported account types**: *Accounts in this organizational directory only*
   - **Redirect URI**: select **Public client/native (mobile & desktop)** and enter `http://localhost:53214`
3. Click **Register** and copy:
   - **Application (client) ID** → `AZURE_CLIENT_ID`
   - **Directory (tenant) ID** → `AZURE_TENANT_ID`
4. Go to **Authentication** > **Advanced settings** > set **Allow public client flows** to **Yes** > **Save**
5. Go to **API permissions** — **User.Read** should already be present (added by default)

> No client secret is needed. The app uses MSAL's `PublicClientApplication`, which is the recommended flow for desktop apps.

To restrict access: go to **Enterprise applications** > find your app > **Properties** > set **Assignment required?** to **Yes**, then add allowed users under **Users and groups**.

### Google (GCP OAuth2)

Default credentials for the shared `voitta-auth` GCP project are embedded in `.env.sample` — copy them to `.env` and you're done. To use your own project instead:

1. Go to [Google Cloud Console](https://console.cloud.google.com/) > **APIs & Services** > **Credentials** > **Create Credentials** > **OAuth client ID**
2. Select **Desktop app** as the application type
3. Set the name to `voitta-auth` and click **Create**
4. Copy **Client ID** → `GOOGLE_CLIENT_ID` and **Client Secret** → `GOOGLE_CLIENT_SECRET`
5. Add `http://localhost:53214` to **Authorized redirect URIs**
6. Go to **OAuth consent screen** and configure:
   - Add scopes: `openid`, `email`, `profile`
   - Add test users if the app is in "Testing" mode

> Google requires a client secret even for desktop (native) apps. This is safe — Google's own documentation treats desktop client secrets as non-confidential, and they are embedded in the distributed app just like the client ID.

### Okta (OIDC)

1. In the [Okta Admin Console](https://your-org-admin.okta.com/), go to **Applications** > **Create App Integration**
2. Select **OIDC - OpenID Connect** and **Native Application**
3. Configure:
   - **App integration name**: `voitta-auth`
   - **Sign-in redirect URI**: `http://localhost:53214`
   - **Grant type**: Authorization Code
   - **Controlled access**: Assign to groups/users as needed
4. Copy:
   - **Client ID** → `OKTA_CLIENT_ID`
   - Your Okta domain (e.g. `dev-123456.okta.com`) → `OKTA_DOMAIN`

> The app uses the default authorization server (`/oauth2/default`). No client secret is needed — Okta native apps use PKCE.

## Quick Start

```bash
# Clone
git clone git@github.com:voitta-ai/voitta-auth.git
cd voitta-auth

# Configure
cp .env.sample .env
# Edit .env with your provider credentials

# Install
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run
python app.py
```

## Configuration

Initial values come from `.env` (see `.env.sample`). After first launch, use the **Settings** menu to update values — they are persisted to `~/.voitta_auth_settings.json` and take precedence over `.env`.

| Variable | Provider | Description |
|----------|----------|-------------|
| `AZURE_TENANT_ID` | Microsoft | Azure AD tenant (directory) ID |
| `AZURE_CLIENT_ID` | Microsoft | Application (client) ID |
| `GOOGLE_CLIENT_ID` | Google | OAuth2 client ID from GCP |
| `GOOGLE_CLIENT_SECRET` | Google | OAuth2 client secret (non-confidential for desktop apps) |
| `OKTA_DOMAIN` | Okta | Okta org domain (e.g. `dev-123456.okta.com`) |
| `OKTA_CLIENT_ID` | Okta | OIDC client ID |
| `REDIRECT_PORT` | All | Local port for OAuth callback (default: `53214`) |
| `PROXY_PORT` | All | Local port for the auth proxy (default: `18765`) |
| `VOITTA_RAG_URL` | All | Upstream voitta-rag URL (default: `https://rag.voitta.ai`) |

### Proxy Headers

The proxy injects per-provider headers for every authenticated provider:

| Header | Description |
|--------|-------------|
| `X-Auth-Token-Microsoft` | Bearer token from Microsoft |
| `X-Auth-Email-Microsoft` | User email from Microsoft Graph |
| `X-Auth-Name-Microsoft` | Display name from Microsoft Graph |
| `X-Auth-Token-Google` | Bearer token from Google |
| `X-Auth-Email-Google` | User email from Google |
| `X-Auth-Name-Google` | Display name from Google |
| `X-Auth-Token-Okta` | Bearer token from Okta |
| `X-Auth-Email-Okta` | User email from Okta |
| `X-Auth-Name-Okta` | Display name from Okta |

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
