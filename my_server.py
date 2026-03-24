from fastmcp import FastMCP
from poke import Poke

client = Poke()
mcp = FastMCP("TopologyTalk")

@mcp.tool
def greet(name: str) -> str:
    return f"Hello, {name}!"

if __name__ == "__main__":
    mcp.run()