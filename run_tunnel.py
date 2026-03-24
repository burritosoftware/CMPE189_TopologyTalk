import asyncio
import argparse
from poketunnel import login, PokeTunnel, TunnelOptions

async def main(host: str, port: int):
    result = await login()
    print("Logged in:", result.token[:10], "...")

    url = f"http://{host}:{port}/mcp"

    tunnel = PokeTunnel(
        TunnelOptions(
            url=url,
            name="TopologyTalk Dev",
        )
    )

    tunnel.on("connected", lambda info: print("connected:", info))
    tunnel.on("disconnected", lambda: print("disconnected"))
    tunnel.on("toolsSynced", lambda result: print("tools:", result["toolCount"]))
    tunnel.on("oauthRequired", lambda info: print("oauth:", info["authUrl"]))
    tunnel.on("error", lambda err: print("error:", err))

    info = await tunnel.start()
    print("Tunnel URL:", info.tunnel_url)

    try:
        await asyncio.Event().wait()
    finally:
        await tunnel.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run PokeTunnel")
    parser.add_argument("--host", default="localhost", help="Host (default: localhost)")
    parser.add_argument("--port", type=int, default=8000, help="Port (default: 8000)")

    args = parser.parse_args()

    asyncio.run(main(args.host, args.port))