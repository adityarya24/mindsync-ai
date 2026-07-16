"""Backward-compatible entrypoint for MCP configs pointing at mindsync_server.py."""

from mindsync.server import main

if __name__ == "__main__":
    main()
