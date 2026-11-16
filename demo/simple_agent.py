#!/usr/bin/env python3
"""
Simple agent demo using pi-sdk (deepseek-harness-sdk).

This demo shows how to:
1. Set up and configure the DeepSeek Harness SDK
2. Run a simple agent task with a user prompt
3. Handle the agent's response
4. Work with named sessions for continued conversation

Prerequisites:
  - A running dsh binary (installed as part of deepseek-harness-runtime-bin)
  - Valid DEEPSEEK_API_KEY or equivalent credentials
  - An explicit DSH_HOME directory

Usage:
  python -m demo.simple_agent --dsh-home /path/to/dsh-home "your task here"
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Import the pi-sdk (deepseek-harness-sdk)
from deepseek_harness import DeepSeekHarness, RunResult


def main() -> None:
    parser = argparse.ArgumentParser(description="Simple agent demo using pi-sdk")
    parser.add_argument(
        "prompt",
        nargs="?",
        default="Say hello and tell me what you can do.",
        help="Task prompt for the agent (default: a greeting task)",
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
        "--provider",
        default="deepseek-official",
        help="LLM provider (default: deepseek-official)",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("DSH_MODEL", "deepseek-v4-flash"),
        help="Model name (default: DSH_MODEL env or deepseek-v4-flash)",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="Session ID to resume a previous conversation",
    )
    parser.add_argument(
        "--dsh-bin",
        type=Path,
        default=None,
        help="Path to dsh binary (auto-detected if not provided)",
    )
    args = parser.parse_args()

    # Validate inputs
    if not args.dsh_home.exists():
        parser.error(f"DSH_HOME directory does not exist: {args.dsh_home}")

    workspace = args.workspace.resolve()
    dsh_home = args.dsh_home.resolve()

    print(f"🚀 Initializing agent demo...")
    print(f"   DSH_HOME:  {dsh_home}")
    print(f"   Workspace: {workspace}")
    print(f"   Provider:  {args.provider}")
    print(f"   Model:     {args.model}")
    print(f"   Prompt:    {args.prompt}")
    print()

    # Create the DeepSeek Harness SDK client
    # The SDK launches a dsh subprocess with the 'sdk' profile
    harness_kwargs = {
        "dsh_home": str(dsh_home),
        "cwd": str(workspace),
        "provider": args.provider,
        "model": args.model,
        "profile": "sdk",
    }
    if args.dsh_bin is not None:
        harness_kwargs["dsh_bin"] = str(args.dsh_bin.resolve())

    with DeepSeekHarness(**harness_kwargs) as harness:  # type: ignore[arg-type]
        # Run the agent task
        # The session.run() method sends the prompt and waits for completion
        print("🤖 Running agent...")
        result: RunResult = harness.run(
            args.prompt,
            session_id=args.session_id,
        )

        # Display results
        print()
        print("=" * 60)
        print("📋 Agent Response:")
        print("=" * 60)
        print(result.final_response)

        if result.finish_reason not in (None, "completed"):
            print()
            print(
                f"⚠️  Warning: Agent finished with reason: {result.finish_reason}",
                file=sys.stderr,
            )

        print()
        print(f"✨ Done (session_id: {result.session_id})")


if __name__ == "__main__":
    main()