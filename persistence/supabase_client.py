"""
Thin Supabase client wrapper.

Uses the SECRET key (server-side, bypasses RLS) since this bot is a
trusted backend process, not a public-facing client app. If a public
dashboard frontend is built later that talks to Supabase directly from
the browser, that should use the PUBLISHABLE key instead with proper
RLS policies - not this client.
"""

from __future__ import annotations

from functools import lru_cache

from supabase import Client, create_client

from config.settings import settings


@lru_cache(maxsize=1)
def get_client() -> Client:
    """Returns a cached Supabase client using the secret (service role) key.
    Cached so we don't reconnect on every call within a process."""
    if not settings.supabase_url or not settings.supabase_secret_key:
        raise RuntimeError(
            "Supabase credentials missing. Check your .env file has "
            "SUPABASE_URL and SUPABASE_SECRET_KEY set."
        )
    return create_client(settings.supabase_url, settings.supabase_secret_key)


if __name__ == "__main__":
    # Manual connectivity check: `python -m persistence.supabase_client`
    # Requires a real .env with valid Supabase credentials.
    client = get_client()
    # Simple query against a table that should exist after running the migration
    result = client.table("signals").select("id").limit(1).execute()
    print("Connected to Supabase successfully.")
    print(f"Sample query returned {len(result.data)} row(s).")
