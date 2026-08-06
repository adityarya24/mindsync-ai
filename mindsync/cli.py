"""CLI entry point for MindSync management subcommands."""

from mindsync.manage import build_parser, main

__all__ = ["build_parser", "main"]

if __name__ == "__main__":
    import sys
    sys.exit(main())
