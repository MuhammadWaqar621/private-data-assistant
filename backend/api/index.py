"""
Entrypoint for Vercel's Python runtime.

Vercel's Python builder auto-detects an ASGI application by looking for a
module-level `app` object - this file just imports and re-exports the real
FastAPI app from app/main.py, unchanged, so nothing about the application
itself needs to know it's running on Vercel. See ../vercel.json for the
rewrite rule that sends every request (not just /api/*) to this function.

Note what does NOT happen here: no `alembic upgrade head`, no schema
creation, nothing at import time beyond what app/main.py already does
(load_dotenv() + building the FastAPI app). Migrations must be run
out-of-band, once, against the provisioned DATABASE_URL - see the root
README's "Deploying to Vercel" section. A serverless function's cold start
is the wrong place to run a migration: multiple instances can start
concurrently, and a slow migration would eat into every request's latency
budget for no benefit once the schema is already up to date.
"""

import sys
from pathlib import Path

# Defensive, mirroring alembic/env.py's own sys.path handling: make sure
# the project root (this file's grandparent - i.e. backend/, which holds
# the `app` package) is importable regardless of exactly how Vercel's
# Python builder sets up sys.path/cwd for this function.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app  # noqa: E402,F401
