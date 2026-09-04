"""
Applies SQL migration files to Supabase.

NOTE: Supabase's Python client doesn't support raw DDL execution directly.
The reliable way to run migrations is via the Supabase Dashboard's SQL
Editor (copy-paste the .sql file contents) or the Supabase CLI. This
script just prints instructions and the file path for convenience.

Usage: python -m scripts.run_migration
"""

from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "persistence" / "migrations"


def main():
    migrations = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not migrations:
        print("No migration files found.")
        return

    print("Found migration file(s):")
    for m in migrations:
        print(f"  - {m}")

    print(
        "\nTo apply: open your Supabase project -> SQL Editor -> New Query, "
        "paste the contents of each file above (in order), and click Run.\n"
        "Alternatively, if you have the Supabase CLI installed: "
        "`supabase db push` (requires project linking first)."
    )


if __name__ == "__main__":
    main()
