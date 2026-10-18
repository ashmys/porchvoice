import argparse
import os

from mcp.server.mcpserver import MCPServer

server = MCPServer(
    name="porchvoice-test-tools",
    version="1.0.0",
    instructions="Tiny MCP server used by the PorchVoice E2E smoke test.",
)


@server.tool()
def add(a: float, b: float) -> float:
    """Add two numbers and return the sum."""
    return a + b


@server.tool()
def echo(text: str) -> str:
    """Return the given text unchanged."""
    return text


@server.tool()
def room_greeting(room: str) -> str:
    """Build a greeting line for a room name."""
    return "Hello from room " + room


class BearerMiddleware:
    def __init__(self, app, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                       for k, v in scope.get("headers", [])}
            if headers.get("authorization", "") != "Bearer " + self.token:
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body",
                            "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9801)
    ap.add_argument("--token", default=os.environ.get("MCP_TEST_TOKEN", ""))
    args = ap.parse_args()

    app = server.streamable_http_app()
    if args.token:
        app = BearerMiddleware(app, args.token)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
