#!/usr/bin/env python3
"""
Conversational agent demo using pi-sdk (deepseek-harness-sdk).

This demo shows how to:
1. Use named sessions to maintain conversation context
2. Send follow-up prompts that build on previous responses
3. Handle user questions during agent execution
4. Optionally configure MCP servers whose tools become available
   to the agent throughout the conversation

Usage::

    python -m demo.conversational_agent --dsh-home /path/to/dsh-home

    # With MCP tools:
    python -m demo.conversational_agent --dsh-home /path/to/dsh-home \\
        --mcp-stdio '{"name":"fetch","command":"npx","args":["-y","@modelcontextprotocol/server-fetch"]}'
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path

from deepseek_harness import DeepSeekHarness, Notification, UserQuestionRequest, RunResult

from demo.mcp import add_mcp_arguments, mcp_servers_from_args, write_patch_file


def handle_user_question(
    request: UserQuestionRequest,
    cancelled: threading.Event,
) -> dict | None:
    """Handle user questions from the agent (e.g. approval requests).

    This example auto-answers with a default response.
    For production use, implement interactive decision-making here.
    """
    for item in request.questions:
        question = item.get("question", "")
        print(f"\n🤔 Agent asks: {question}", file=sys.stderr)
    return None  # Let the runtime handle unanswered questions


def print_agent_response(result: RunResult) -> None:
    """Print the agent's final response and session info."""
    print()
    print("=" * 60)
    print("📋 Agent Response:")
    print("=" * 60)
    print(result.final_response)
    print()
    print(f"✅ Finish reason: {result.finish_reason}")
    print(f"🔑 Session ID: {result.session_id}")
    print(f"📊 Events received: {len(result.events)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Conversational agent demo using pi-sdk"
    )
    parser.add_argument(
        "--dsh-home",
        type=Path,
        required=True,
        help="Path to the DeepSeek Harness home directory",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Working directory for the agent (default: current directory)",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("DSH_MODEL", "deepseek-v4-flash"),
        help="Model name (default: DSH_MODEL env or deepseek-v4-flash)",
    )
    parser.add_argument(
        "--session-id",
        default="conversational-demo",
        help="Session ID for maintaining conversation context",
    )
    parser.add_argument(
        "--provider",
        default=os.environ.get("DSH_PROVIDER", "deepseek-official"),
        help="LLM provider (default: DSH_PROVIDER env or deepseek-official)",
    )
    # MCP server configuration
    add_mcp_arguments(parser)
    args = parser.parse_args()

    if not args.dsh_home.exists():
        parser.error(f"DSH_HOME directory does not exist: {args.dsh_home}")

    workspace = args.workspace.resolve()
    dsh_home = args.dsh_home.resolve()

    # Resolve MCP servers
    servers = mcp_servers_from_args(args)
    patches: list[str] = []
    if args.mcp_patch is not None:
        patches.append(str(args.mcp_patch.resolve()))
    if servers:
        patch_path = write_patch_file(servers, prefix="mcp-conversational-")
        print(f"🔌 MCP servers configured: {[s.name for s in servers]}")
        print(f"   Patch file: {patch_path}")
        patches.append(str(patch_path))
        setattr(args, "_mcp_patch_path", patch_path)

    # Configure the DeepSeek Harness SDK
    config = {
        "dsh_home": str(dsh_home),
        "cwd": str(workspace),
        "provider": args.provider,
        "model": args.model,
        "profile": "sdk",
        "patches": tuple(patches),
        "on_user_question": handle_user_question,
    }

    print("🚀 Starting conversational agent demo")
    print(f"   DSH_HOME:  {dsh_home}")
    print(f"   Session:   {args.session_id}")
    print()

    with DeepSeekHarness(**config) as harness:  # type: ignore[arg-type]
        # ---- Turn 1: First question ----
        prompt1 = "List 3 interesting facts about the Python programming language."
        print(f"💬 Turn 1: {prompt1}")
        result1 = harness.run(prompt1, session_id=args.session_id)
        print_agent_response(result1)

        # ---- Turn 2: Follow-up (context preserved) ----
        prompt2 = "For each fact, explain why it makes Python unique among programming languages."
        print(f"\n💬 Turn 2: {prompt2}")
        result2 = harness.run(prompt2, session_id=args.session_id)
        print_agent_response(result2)

        # ---- Turn 3: Task-oriented prompt ----
        prompt3 = "Write a simple Python function that calculates the Fibonacci sequence."
        print(f"\n💬 Turn 3: {prompt3}")
        result3 = harness.run(prompt3, session_id=args.session_id)
        print_agent_response(result3)

    print()
    print("✨ Conversational demo completed successfully!")
    print(f"   Session saved. Resume with --session-id {args.session_id}")

    # Cleanup temporary MCP patch file
    mcp_patch = getattr(args, "_mcp_patch_path", None)
    if mcp_patch is not None:
        try:
            mcp_patch.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()
