services:
  jellyfin-mcp:
    build: .
    ports:
      - "8000:8000"
    environment:
      - JELLYFIN_URL=http://nas.local:8096
      - JELLYFIN_USER=me
      - JELLYFIN_DEFAULT_LIMIT=50
      - JELLYFIN_API_KEY=your-api-key-here
      # - MCP_PORT=8000          # override if port 8000 is taken
    restart: unless-stopped
