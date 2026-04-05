# Companion MCP Server Configuration

## Goal

Enable AI assistants (Claude Code) to programmatically control both Bitfocus Companion instances (companion.lan and companion-pp.lan) via the [companion-mcp-server](https://github.com/yannisgu/companion-mcp-server) MCP server.

## Context

Companion exposes an HTTP API on port 8889. The companion-mcp-server is a TypeScript MCP server that wraps this API, exposing tools for managing buttons, pages, triggers, variables, and connections. Both production Companion containers run with `network_mode: host`, so port 8889 is accessible on the host network.

## Design

### What changes

Create `.mcp.json` in the repo root with two MCP server entries:

- **companion-snv** — connects to `http://companion.lan:8889` (the primary/SNV instance)
- **companion-pp** — connects to `http://companion-pp.lan:8889` (the secondary/PP instance)

Both entries use `npx companion-mcp-server` with the Companion URL passed as a command-line argument.

### .mcp.json format

```json
{
  "mcpServers": {
    "companion-snv": {
      "command": "npx",
      "args": ["companion-mcp-server", "http://companion.lan:8889"]
    },
    "companion-pp": {
      "command": "npx",
      "args": ["companion-mcp-server", "http://companion-pp.lan:8889"]
    }
  }
}
```

### What this enables

Once configured, Claude Code sessions in this project will have access to MCP tools prefixed with `mcp__companion-snv__` and `mcp__companion-pp__` for controlling each Companion instance. Available tools include:

- Variable management (list, search, get, create, set)
- Connection listing and inspection
- Page management (list, create, remove, rename, move, clear)
- Button control (get, create, update, delete, press)
- Trigger management (list, get, create, clone, update, delete, batch update)

### Prerequisites

- Node.js 18+ and npm must be available on the dev machine (for npx)
- Both `companion.lan` and `companion-pp.lan` must be reachable on port 8889 from the dev machine

### Files

- Create: `.mcp.json` (repo root)
- No changes to Docker, deploy.sh, or any existing files

### Verification

1. Confirm port 8889 is reachable on both machines
2. After creating `.mcp.json`, restart Claude Code (or start a new session) to pick up the MCP config
3. Verify MCP tools are available by listing variables or connections on each instance
