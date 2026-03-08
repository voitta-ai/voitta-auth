# Voitta Auth

A macOS menu bar application that authenticates users via multiple identity providers (Microsoft, Google, Okta) and runs a unified [FastMCP](https://gofastmcp.com) proxy that mounts multiple MCP backends under a single endpoint, injecting credentials where necessary. Designed for use with [Claude Code](https://claude.com/claude-code).

## How It Works

1. Sits in the macOS menu bar showing `M G O J` (RAG providers + Jira) and circled `(M) (G)` (Edit providers). Bright when authenticated/configured, dimmed when not. Adapts to the system light/dark theme
2. Each provider can be activated/deactivated independently
3. On activation, opens the provider's login page in the browser and captures the OAuth2 callback
4. Runs a unified FastMCP proxy (default `http://127.0.0.1:18765/mcp`) that mounts three backends:
   - **voitta_rag** — forwards to [voitta-rag](https://github.com/voitta-ai/voitta-rag), injects per-provider `X-Auth-Token-*` headers
   - **google_workspace** — forwards to a Google Workspace MCP server, injects `Authorization: Bearer` header
   - **jira** — forwards to mcp-atlassian subprocess (credentials via `.env`)
5. Edit providers use broader OAuth scopes for document editing (Sheets, Docs, Slides, Drive for Google; Files, Sites for Microsoft)
6. Jira uses static credentials (email + API token) — no browser login or token refresh needed
7. OAuth tokens are held in memory and refreshed automatically while the app is running

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
6. For **Microsoft Edit** (document editing), also add: **Files.ReadWrite.All** and **Sites.ReadWrite.All** (these require admin consent)

> No client secret is needed. The app uses MSAL's `PublicClientApplication`, which is the recommended flow for desktop apps.

To restrict access: go to **Enterprise applications** > find your app > **Properties** > set **Assignment required?** to **Yes**, then add allowed users under **Users and groups**.

> **Edit providers** can use the same app registration as the RAG provider (credentials default to the same values). To use a separate registration, override the Edit credentials in Settings.

### Google (GCP OAuth2)

Default credentials for the shared `voitta-auth` GCP project are embedded in `.env.sample` — copy them to `.env` and you're done. To use your own project instead:

1. Go to [Google Cloud Console](https://console.cloud.google.com/) > **APIs & Services** > **Credentials** > **Create Credentials** > **OAuth client ID**
2. Select **Desktop app** as the application type
3. Set the name to `voitta-auth` and click **Create**
4. Copy **Client ID** → `GOOGLE_CLIENT_ID` and **Client Secret** → `GOOGLE_CLIENT_SECRET`
5. Add `http://localhost:53214` to **Authorized redirect URIs**
6. Go to **OAuth consent screen** and configure:
   - Add scopes: `openid`, `email`, `profile`
   - For **Google Edit**, also add: `spreadsheets`, `documents`, `presentations`, `drive`
   - Add test users if the app is in "Testing" mode

> Google requires a client secret even for desktop (native) apps. This is safe — Google's own documentation treats desktop client secrets as non-confidential, and they are embedded in the distributed app just like the client ID.

> **Edit providers** can use the same GCP project as the RAG provider (credentials default to the same values). To use a separate project, override the Edit credentials in Settings.

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

### Jira (Atlassian)

1. Go to [Atlassian Account Settings](https://id.atlassian.com/manage-profile/security/api-tokens) > **Create API token**
2. Copy the token → `JIRA_API_TOKEN`
3. Use the email address of your Atlassian account → `JIRA_EMAIL`

> No OAuth flow — voitta-auth stores the email and API token and injects them as `Authorization: Basic base64(email:token)` into every request to the Atlassian MCP server. The `J` indicator in the menu bar is bright when both fields are configured.

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
| `PROXY_PORT` | All | Local port for the unified MCP proxy (default: `18765`) |
| `VOITTA_RAG_URL` | RAG | Upstream voitta-rag URL (default: `https://rag.voitta.ai`) |
| `EDIT_PROXY_URL` | Edit | Upstream Google Workspace MCP URL (default: `http://localhost:8000`) |
| `EDIT_MCP_ENV_PATH` | Edit | Path to the workspace MCP server's `.env` file (default: `~/DEVEL/google_workspace_mcp/.env`) |
| `JIRA_EMAIL` | Jira | Atlassian account email |
| `JIRA_API_TOKEN` | Jira | Atlassian API token |
| `JIRA_MCP_PORT` | Jira | Port for the mcp-atlassian subprocess (default: `18767`) |

Edit provider credentials default to the same values as the corresponding RAG provider. Override them in Settings to use separate OAuth apps.

### RAG Backend Headers

The `voitta_rag` proxy backend injects per-provider headers for every authenticated RAG provider:

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

### Google Workspace Backend Headers

The `google_workspace` proxy backend injects a single standard `Authorization` header from the first available edit provider:

| Header | Description |
|--------|-------------|
| `Authorization` | `Bearer <token>` from the first authenticated edit provider |
| `X-Auth-Email` | User email |
| `X-Auth-Name` | Display name |

This is designed for use with [google_workspace_mcp](https://github.com/taylorwilsdon/google_workspace_mcp) in `EXTERNAL_OAUTH21_PROVIDER=true` mode, or any MCP server that accepts standard Bearer tokens.

### Jira Backend

The `jira` proxy backend forwards to the local mcp-atlassian subprocess (port 18767). Jira credentials are passed to mcp-atlassian via its `.env` file — no header injection needed at the proxy level.

### Automatic MCP Server Configuration

When `EDIT_MCP_ENV_PATH` is set (default: `~/DEVEL/google_workspace_mcp/.env`), voitta-auth automatically writes the Google Edit OAuth credentials to the workspace MCP server's `.env` file. This means the MCP server requires zero manual credential configuration — just clone it and run:

```bash
git clone https://github.com/taylorwilsdon/google_workspace_mcp.git
cd google_workspace_mcp
uv sync
uv run main.py --transport streamable-http
```

voitta-auth writes `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `MCP_ENABLE_OAUTH21=true`, and `EXTERNAL_OAUTH21_PROVIDER=true` to the `.env` on startup and whenever credentials change.

### Claude Code MCP Setup

Add the following to your `claude_desktop_config.json` or `.claude/settings.json` (or project-level `.mcp.json`):

```json
{
  "mcpServers": {
    "voitta": {
      "url": "http://127.0.0.1:18765/mcp"
    }
  }
}
```

All backends are exposed through this single endpoint. Tools are namespaced with prefixes: `voitta_rag_*`, `google_workspace_*`, `jira_*`.

## License

This project is licensed under the [GNU Affero General Public License v3.0](LICENSE).
