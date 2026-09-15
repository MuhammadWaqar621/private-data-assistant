"""
Vercel serverless entry point for the FastAPI backend.

Vercel's Python runtime auto-detects an ASGI application named `app` in
any module it's pointed at (via vercel.json's `functions` config) and
wraps it as a serverless function - it does not run `uvicorn` itself.
The repo-root vercel.json rewrites every /api/* request to this one
function, so FastAPI's own routing in app/main.py (every real route is
already prefixed with /api/...) takes over from there.

Not used by local development - `uvicorn app.main:app` (see README) still
runs the app directly. This file only exists for the Vercel deployment.
"""

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent / "backend"))

from app.main import app  # noqa: E402

__all__ = ["app"]
