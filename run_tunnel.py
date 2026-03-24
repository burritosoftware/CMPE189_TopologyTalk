import asyncio
from poketunnel import login, PokeTunnel, TunnelOptions

async def main():
    result = await login()
    print("Logged in:", result.token[:10], "...")

    tunnel = PokeTunnel(
        TunnelOptions(
            url="http://localhost:8000/mcp",
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

asyncio.run(main())