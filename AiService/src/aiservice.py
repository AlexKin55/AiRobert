"""Legacy alias entry point.

The real entry point is src/server.py (python3 -m src.server). This module
only re-exports the FastAPI app and main() for backward compatibility.
"""
from .server import app, main  # noqa: F401

if __name__ == "__main__":
    main()