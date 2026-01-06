# Parallel-Poke MCP

A bridge between [Parallel's Monitor API](https://docs.parallel.ai/monitor-api) and [Poke](https://poke.com) - get SMS notifications when your web monitors detect changes, and manage monitors conversationally through Poke.

## What It Does

1. **Receives webhooks from Parallel** when your monitors detect web changes
2. **Forwards event details to Poke** which sends you an SMS notification
3. **Exposes MCP tools** so Poke can create/manage monitors on your behalf

This means you can text Poke things like:
- "Create a monitor for React changelog updates, check daily"
- "What monitors do I have?"
- "Show me recent events"
- "Delete the OpenAI monitor"

And you'll automatically get SMS notifications when any monitor detects changes.

## Deploy

### Option 1: Render (Recommended)

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/konarkm/parallel-poke-mcp)

### Option 2: Railway

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/new/template?template=https://github.com/konarkm/parallel-poke-mcp)

### Option 3: Manual Deployment

1. Fork this repository
2. Deploy to any platform that supports Python (Render, Railway, Fly.io, etc.)
3. Set the environment variables (see below)
4. Your server will be available at `https://your-app-url.com/mcp`

## Configuration

Set these environment variables in your deployment platform:

| Variable | Required | Description |
|----------|----------|-------------|
| `PARALLEL_API_KEY` | Yes | Your Parallel API key from [platform.parallel.ai](https://platform.parallel.ai) |
| `POKE_API_KEY` | Yes | Your Poke API key from [poke.com/settings/advanced](https://poke.com/settings/advanced) |
| `WEBHOOK_BASE_URL` | Yes | Your deployed server URL (e.g., `https://your-app.onrender.com`) |
| `PARALLEL_WEBHOOK_SECRET` | No | Webhook signing secret for verification (recommended) |
| `MCP_AUTH_TOKEN` | No | If set, MCP clients must send `Authorization: Bearer <token>` |

## Connect to Poke

1. Deploy the server and note your URL (e.g., `https://parallel-poke-mcp.onrender.com`)
2. Go to [poke.com/settings/connections](https://poke.com/settings/connections)
3. Add a new MCP integration with URL: `https://your-url.com/mcp`
4. Test by texting Poke: "List my Parallel monitors"

## Endpoints

| Endpoint | Purpose |
|----------|---------|
| `/mcp` | MCP protocol endpoint (Poke connects here) |
| `/webhook/parallel` | Receives webhooks from Parallel monitors |
| `/health` | Health check for load balancers |

## MCP Auth (Optional)

If you set `MCP_AUTH_TOKEN`, the MCP endpoint requires a bearer token:

```
Authorization: Bearer <your token>
```

If your MCP client supports auth (e.g., Poke), configure the same token there.

## MCP Tools

The server exposes these tools to Poke:

| Tool | Description |
|------|-------------|
| `create_monitor` | Create a new monitor with a query and cadence (hourly/daily/weekly) |
| `list_monitors` | List all your active monitors |
| `get_monitor` | Get details of a specific monitor |
| `update_monitor` | Update a monitor (cadence, webhook, metadata) |
| `delete_monitor` | Delete a monitor |
| `list_recent_events` | Get recent events across monitors |

Webhook event types supported by the API:
- `monitor.event.detected`
- `monitor.execution.completed`
- `monitor.execution.failed`

Use `update_monitor` to set `webhook_event_types` if you want completion/failure notifications.
Metadata values must be strings (Parallel API requirement).

## Local Development

```bash
# Clone the repo
git clone https://github.com/YOUR_USERNAME/parallel-poke-mcp.git
cd parallel-poke-mcp

# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows

# Install dependencies
python3 -m pip install -r requirements.txt

# Set environment variables
cp .env.example .env
# Edit .env with your API keys

# Run the server
python3 src/server.py
```

Test with the MCP Inspector:
```bash
npx @modelcontextprotocol/inspector
```

Connect to `http://localhost:8000/mcp` using "Streamable HTTP" transport.

## How It Works

```
┌─────────────────────┐
│  Parallel Monitors  │  You define what to watch
│  (hourly/daily/wk)  │  "React changelog updates"
└─────────┬───────────┘
          │ webhook fires when changes detected
          ▼
┌─────────────────────┐
│  This MCP Server    │  Receives webhook, fetches
│  (Render/Railway)   │  full event details
└─────────┬───────────┘
          │ forwards to Poke API
          ▼
┌─────────────────────┐
│       Poke          │  AI formats and sends
└─────────┬───────────┘  SMS notification
          │
          ▼
        [SMS]
```

Plus, Poke can call the MCP tools to manage monitors:

```
You: "Create a monitor for Vercel changelog, check weekly"
          │
          ▼
┌─────────────────────┐
│       Poke          │  Calls create_monitor tool
└─────────┬───────────┘
          │ MCP tool call
          ▼
┌─────────────────────┐
│  This MCP Server    │  Calls Parallel API
└─────────┬───────────┘  to create monitor
          │
          ▼
┌─────────────────────┐
│  Parallel API       │  Monitor created!
└─────────────────────┘
```

## Example Use Cases

- **Changelog monitoring**: Track updates to tools you use (React, Next.js, etc.)
- **News alerts**: Monitor announcements from companies you follow
- **Price tracking**: Watch for deals on products
- **Competitor monitoring**: Track competitor announcements or launches
- **Job postings**: Monitor career pages at companies you're interested in

## Troubleshooting

**Poke isn't calling my MCP tools**
- Make sure the MCP URL ends with `/mcp` (e.g., `https://your-app.onrender.com/mcp`)
- Try sending `clearhistory` to Poke to reset conversation state
- Ask Poke explicitly: "Use the Parallel Monitor integration to list monitors"

**Webhooks aren't working**
- Check that `WEBHOOK_BASE_URL` is set correctly (no trailing slash)
- Verify your Parallel webhook secret matches if you're using signature verification
- Check the `/health` endpoint to confirm the server is running

**Monitor creation fails**
- Ensure `PARALLEL_API_KEY` is set correctly
- Check that `WEBHOOK_BASE_URL` is accessible from the internet

## License

MIT
