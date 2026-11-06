"""Separate console process. GUI launches it with stdio pipes via the MCP SDK."""
import os

# Set only this helper's environment before upstream initializes HTTP clients.
from ptagent.bridge import local_bridge_env
os.environ.update(local_bridge_env(os.environ))

from packet_tracer_mcp.server import main

if __name__ == "__main__":
    main()
