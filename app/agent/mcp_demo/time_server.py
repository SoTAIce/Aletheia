from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("CurrentTime")

@mcp.tool()
def get_current_time(timezone: str) -> str:
    """Return the current time in an IANA timezone (e.g. Asia/Shanghai) as ISO 8601."""
    try:
        tz = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as e:
        raise ValueError(
            f"Unknown timezone: {timezone}"
        ) from e
    current_time = datetime.now(tz)

    return current_time.isoformat()

if __name__ == "__main__":
    mcp.run(transport="stdio")