"""
MCP (Model Context Protocol) server configuration for pi-sdk agent demos.

Provides utilities to configure MCP servers and generate the cordis patch
YAML that provisions ``@deepseek-ai/dsh-mcp-client`` plugin instances for
the dsh runtime. Each MCP server exposes its tools to the agent under a
server-qualified name (``mcp__<serverName>__<rawName>``).

Usage::

    from demo.mcp import StdioServer, generate_yaml

    servers = [
        StdioServer(
            name="filesystem",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        ),
    ]
    yaml_text = generate_yaml(servers)
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar


# ── Server configuration ────────────────────────────────────────────────────


@dataclass
class StdioServer:
    """An MCP server started as a child process over stdio.

    Args:
        name: Stable local namespace for this server's tool names
            (``mcp__<name>__<rawToolName>``). Must match ``[A-Za-z0-9_-]{1,32}``.
        command: Executable used to start the server.
        args: Arguments passed directly, without shell interpolation.
        env: Extra environment variables merged on top of the dsh runtime's environment.
        cwd: Working directory for the child process (empty = inherit dsh working dir).
        tool_call_timeout_ms: Per-tool-call timeout in milliseconds (default 60000).
        startup_timeout_ms: Connection attempt + initial tool sync budget (default 30000).
        fail_on_startup_error: When ``True``, the plugin activation fails if the server
            cannot be reached (default ``False`` so a broken server does not block the agent).
    """

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    tool_call_timeout_ms: int = 60_000
    startup_timeout_ms: int = 30_000
    fail_on_startup_error: bool = False

    _transport: ClassVar[str] = "stdio"

    def to_dict(self) -> dict:
        """Return a dict suitable as a YAML mapping body."""
        return {
            "transport": "stdio",
            "serverName": self.name,
            "command": self.command,
            "args": list(self.args),
            "env": dict(self.env),
            "cwd": self.cwd,
            "toolCallTimeoutMs": self.tool_call_timeout_ms,
            "startupTimeoutMs": self.startup_timeout_ms,
            "failOnStartupError": self.fail_on_startup_error,
        }


@dataclass
class StreamableHttpServer:
    """An MCP server connected over Streamable HTTP (SSE).

    Args:
        name: Stable local namespace for this server's tool names.
        url: MCP endpoint URL.
        headers: Extra HTTP headers attached to MCP requests.
        tool_call_timeout_ms: Per-tool-call timeout in milliseconds (default 60000).
        startup_timeout_ms: Connection attempt + initial tool sync budget (default 30000).
        fail_on_startup_error: When ``True``, plugin activation fails if unreachable.
    """

    name: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    tool_call_timeout_ms: int = 60_000
    startup_timeout_ms: int = 30_000
    fail_on_startup_error: bool = False

    _transport: ClassVar[str] = "streamable-http"

    def to_dict(self) -> dict:
        """Return a dict suitable as a YAML mapping body."""
        return {
            "transport": "streamable-http",
            "serverName": self.name,
            "url": self.url,
            "headers": dict(self.headers),
            "toolCallTimeoutMs": self.tool_call_timeout_ms,
            "startupTimeoutMs": self.startup_timeout_ms,
            "failOnStartupError": self.fail_on_startup_error,
        }


# ── YAML generation (no PyYAML dependency) ──────────────────────────────────


def _yaml_value(value: object, indent: int = 0) -> str:
    """Format a Python value as a YAML literal (flow scalars and minimal block)."""
    pad = "  " * indent
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        # Quote strings that could be misinterpreted
        if not value or value[0] in ("'", '"', "&", "*", "!", "|", ">", "%", "@", "`") or any(
            ch in value for ch in (":", "#", "{", "}", "[", "]", ",", "&", "*", "?", "|", "-", "<", ">", "=", "!", "%", "@", "`")
        ):
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'
        return value
    if isinstance(value, list):
        if not value:
            return "[]"
        lines = [f"{pad}- {_yaml_value(item, indent)}" for item in value]
        return "\n".join(lines)
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = []
        for k, v in value.items():
            lines.append(f"{pad}{k}: {_yaml_value(v, indent)}")
        return "\n".join(lines)
    if isinstance(value, type) and value is str:
        return "String"
    return str(value)


def _entry_to_yaml(id: str, config: dict) -> str:
    """Format a single mcp-client cordis plugin entry."""
    lines = [f"    - id: {id}", "      name: '@deepseek-ai/dsh-mcp-client'", "      config:"]
    for key, val in config.items():
        # Skip defaults; only emit user-supplied values for clarity
        rendered = _yaml_value(val, indent=4)
        if "\n" in rendered:
            lines.append(f"        {key}:")
            for line in rendered.splitlines(keepends=False):
                lines.append(f"          {line.lstrip()}")
        else:
            lines.append(f"        {key}: {rendered}")
    return "\n".join(lines)


def generate_yaml(servers: list[StdioServer | StreamableHttpServer]) -> str:
    """Generate a cordis patch YAML string that provisions all *servers*.

    The generated YAML inserts ``@deepseek-ai/dsh-mcp-client`` instances,
    one per server, under a top-level ``- insert:`` list that the cordis
    loader accepts as a patch file.  The resulting text can be written to
    a ``.yml`` file and passed via ``--patch <path>`` to the dsh runtime,
    or piped through the ``patches`` parameter of
    :class:`~deepseek_harness.DeepSeekHarnessConfig`.

    Returns:
        YAML text suitable for a cordis patch file.
    """
    lines = [
        "# Auto-generated MCP server patch for pi-sdk agent",
        "#",
        "- insert:",
    ]
    for i, server in enumerate(servers):
        lines.append(_entry_to_yaml(f"mcp-server-{i}", server.to_dict()))
    lines.append("")
    return "\n".join(lines)


def write_patch_file(servers: list[StdioServer | StreamableHttpServer], *, prefix: str = "mcp-patch") -> Path:
    """Write *servers* to a temporary patch file and return its path.

    The caller is responsible for cleanup (the file is created in the system
    temporary directory with ``delete=False``).

    Args:
        servers: One or more MCP server configurations.
        prefix: Prefix for the temporary file name.

    Returns:
        Absolute path of the written patch YAML file.
    """
    yaml_text = generate_yaml(servers)
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".yml",
        prefix=prefix,
        delete=False,
        encoding="utf-8",
    ) as f:
        f.write(yaml_text)
        return Path(f.name)


def add_mcp_arguments(parser) -> None:
    """Add ``--mcp-stdio`` and ``--mcp-http`` arguments to *parser*.

    Intended for use in demo agent argument parsers::

        parser = argparse.ArgumentParser(...)
        add_mcp_arguments(parser)
        args = parser.parse_args()
        servers = mcp_servers_from_args(args)
    """
    parser.add_argument(
        "--mcp-stdio",
        action="append",
        default=[],
        metavar="JSON",
        help=(
            "Configure one stdio MCP server as a JSON object: "
            '{"name":"my-tool","command":"npx","args":["-y","pkg"]}. '
            "Can be specified multiple times for multiple servers."
        ),
    )
    parser.add_argument(
        "--mcp-http",
        action="append",
        default=[],
        metavar="JSON",
        help=(
            "Configure one Streamable HTTP MCP server as a JSON object: "
            '{"name":"my-api","url":"http://localhost:8080/mcp"}. '
            "Can be specified multiple times for multiple servers."
        ),
    )
    parser.add_argument(
        "--mcp-patch",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Path to a pre-written cordis patch YAML file that provisions "
            "MCP servers. Useful when you want to use a static patch file "
            "instead of the dynamic JSON-based configuration."
        ),
    )


def mcp_servers_from_args(args) -> list[StdioServer | StreamableHttpServer]:
    """Build a server list from parsed ``--mcp-stdio`` and ``--mcp-http`` args.

    Returns:
        A new list of :class:`StdioServer` and :class:`StreamableHttpServer`
        instances.  An empty list when no MCP arguments were provided.
    """
    servers: list[StdioServer | StreamableHttpServer] = []

    for json_str in getattr(args, "mcp_stdio", []) or []:
        obj = json.loads(json_str)
        servers.append(
            StdioServer(
                name=obj["name"],
                command=obj["command"],
                args=obj.get("args", []),
                env=obj.get("env", {}),
                cwd=obj.get("cwd", ""),
                tool_call_timeout_ms=obj.get("toolCallTimeoutMs", 60_000),
                startup_timeout_ms=obj.get("startupTimeoutMs", 30_000),
                fail_on_startup_error=obj.get("failOnStartupError", False),
            )
        )

    for json_str in getattr(args, "mcp_http", []) or []:
        obj = json.loads(json_str)
        servers.append(
            StreamableHttpServer(
                name=obj["name"],
                url=obj["url"],
                headers=obj.get("headers", {}),
                tool_call_timeout_ms=obj.get("toolCallTimeoutMs", 60_000),
                startup_timeout_ms=obj.get("startupTimeoutMs", 30_000),
                fail_on_startup_error=obj.get("failOnStartupError", False),
            )
        )

    return servers
