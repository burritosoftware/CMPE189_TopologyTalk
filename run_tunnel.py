"""
Async helper started alongside my_server: opens a PokeTunnel to the local FastMCP HTTP endpoint.

Why this exists:
  - FastMCP listens on MCP_HOST:MCP_PORT (often 0.0.0.0:8000) for JSON-RPC tool calls.
  - PokeTunnel authenticates with poke.com and forwards remote HTTPS traffic to that URL,
    so an instructor or teammate can attach an MCP client from outside the lab network.

The tunnel runs until the process is stopped; my_server spawns this script with Popen.
"""
import asyncio
import argparse
from poketunnel import login, PokeTunnel, TunnelOptions

async def main(host: str, port: int):
    # Device login flow stores token on disk for subsequent API calls.
    result = await login()
    print("Logged in:", result.token[:10], "...")

    # FastMCP HTTP transport default path (see my_server mcp.run).
    url = f"http://{host}:{port}/mcp"

    tunnel = PokeTunnel(
        TunnelOptions(
            url=url,
            name="TopologyTalk Dev",
        )
    )

    # Optional visibility during bring-up; PokeTunnel emits lifecycle events for UI or logging.
    tunnel.on("connected", lambda info: print("connected:", info))
    tunnel.on("disconnected", lambda: print("disconnected"))
    tunnel.on("toolsSynced", lambda result: print("tools:", result["toolCount"]))
    tunnel.on("oauthRequired", lambda info: print("oauth:", info["authUrl"]))
    tunnel.on("error", lambda err: print("error:", err))

    info = await tunnel.start()
    print("Tunnel URL:", info.tunnel_url)

    try:
        # Block forever until Ctrl+C / SIGTERM; parent process may outlive this if not coordinated.
        await asyncio.Event().wait()
    finally:
        await tunnel.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run PokeTunnel")
    parser.add_argument("--host", default="localhost", help="Host (default: localhost)")
    parser.add_argument("--port", type=int, default=8000, help="Port (default: 8000)")

    args = parser.parse_args()

    asyncio.run(main(args.host, args.port))