"""Create the isolated tester account used by the Playwright acceptance protocol.

This script is local-development infrastructure. It never touches a real account: all rows are
scoped to the reserved context/character ids below, and every run resets those rows first.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, ".")

from app.db import get_connection
from app.esi import ensure_char_tables
from app.features import ensure_features_table
from app.industry.orders import ensure_industry_orders_table
from app.industry.settings import ensure_industry_settings_table
from app.markets import ensure_market_config_table
from app.reactions.jobs import (
    ensure_industry_jobs_table, ensure_reaction_assignments_table,
    ensure_reaction_manual_done_table, ensure_reaction_orders_table,
)

CONTEXT_ID = 997_001
CHARACTER_ID = 997_001_001
CHARACTER_NAME = "Browser Protocol Tester"
SESSION_TOKEN = "eve-pi-browser-protocol-local"
FEATURE_BACKUP = Path("/tmp/eve-pi-browser-feature-backup.json")
FEATURE_OVERRIDES = ("industry_job_length_policy", "reactions_cadence")


def restore_features():
    if not FEATURE_BACKUP.exists():
        return
    original = json.loads(FEATURE_BACKUP.read_text())
    con = get_connection()
    try:
        for key, state in original.items():
            con.execute("UPDATE pp_features SET state=? WHERE key=?", (state, key))
        con.commit()
    finally:
        con.close()
    FEATURE_BACKUP.unlink()
    print("Restored browser protocol feature states")


def seed():
    ensure_char_tables()
    ensure_features_table()
    ensure_industry_orders_table()
    ensure_industry_settings_table()
    ensure_market_config_table()
    ensure_reaction_orders_table()
    ensure_reaction_assignments_table()
    ensure_reaction_manual_done_table()
    ensure_industry_jobs_table()

    con = get_connection()
    try:
        original = {r["key"]: r["state"] for r in con.execute(
            "SELECT key,state FROM pp_features WHERE key IN (?,?)", FEATURE_OVERRIDES)}
        FEATURE_BACKUP.write_text(json.dumps(original))
        for key in FEATURE_OVERRIDES:
            con.execute("UPDATE pp_features SET state='testers' WHERE key=?", (key,))
        # Reset only the reserved fixture tenant. Child rows go first on both SQLite and Postgres.
        con.execute("DELETE FROM pp_reaction_manual_done WHERE context_id=?", (CONTEXT_ID,))
        con.execute("DELETE FROM pp_char_industry_jobs WHERE character_id=?", (CHARACTER_ID,))
        con.execute("DELETE FROM pp_reaction_assignments WHERE character_id=?", (CHARACTER_ID,))
        con.execute("DELETE FROM pp_reaction_order_sources WHERE context_id=?", (CONTEXT_ID,))
        con.execute("DELETE FROM pp_reaction_orders WHERE context_id=?", (CONTEXT_ID,))
        con.execute("DELETE FROM pp_industry_orders WHERE context_id=?", (CONTEXT_ID,))
        con.execute("DELETE FROM pp_industry_settings WHERE context_id=?", (CONTEXT_ID,))
        con.execute("DELETE FROM pp_sessions WHERE context_id=?", (CONTEXT_ID,))
        con.execute("DELETE FROM pp_market_config WHERE context_id=?", (CONTEXT_ID,))
        con.execute("DELETE FROM pp_characters WHERE context_id=?", (CONTEXT_ID,))

        con.execute(
            "INSERT INTO pp_characters "
            "(character_id, character_name, context_id, scopes, mass_reactions, "
            "advanced_mass_reactions, mass_production, advanced_mass_production, industry, "
            "advanced_industry, interplanetary_consolidation, command_center_upgrades) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (CHARACTER_ID, CHARACTER_NAME, CONTEXT_ID,
             "esi-skills.read_skills.v1 esi-industry.read_character_jobs.v1 "
             "esi-characters.read_blueprints.v1 esi-assets.read_assets.v1", 5, 5, 5, 5, 5, 5, 5, 5),
        )
        con.execute(
            "INSERT INTO pp_sessions (token, character_id, context_id, created_at) VALUES (?,?,?,?)",
            (SESSION_TOKEN, CHARACTER_ID, CONTEXT_ID, datetime.now(timezone.utc).isoformat()),
        )
        con.execute(
            "INSERT INTO pp_market_config (context_id, market_character_id, onboarded) VALUES (?,?,1)",
            (CONTEXT_ID, CHARACTER_ID),
        )
        con.execute(
            "INSERT INTO pp_industry_settings (context_id, onboarded, updated_at) VALUES (?,1,?) "
            "ON CONFLICT(context_id) DO UPDATE SET onboarded=1, updated_at=excluded.updated_at",
            (CONTEXT_ID, datetime.now(timezone.utc).timestamp()),
        )
        con.execute(
            "INSERT INTO pp_testers (character_name, added_by, added_at) VALUES (?,?,?) "
            "ON CONFLICT (character_name) DO UPDATE SET added_by=excluded.added_by, added_at=excluded.added_at",
            (CHARACTER_NAME, "browser protocol", datetime.now(timezone.utc).isoformat()),
        )
        # Local feature rungs may differ from dev. Admin+tester exposes every non-hidden protocol
        # surface without rewriting global pp_features state for other local accounts.
        con.execute(
            "INSERT INTO pp_admins (character_name, added_by, added_at) VALUES (?,?,?) "
            "ON CONFLICT (character_name) DO UPDATE SET added_by=excluded.added_by, added_at=excluded.added_at",
            (CHARACTER_NAME, "browser protocol", datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
    finally:
        con.close()

    print(f"Seeded browser protocol context {CONTEXT_ID}; session={SESSION_TOKEN}")


if __name__ == "__main__":
    if "--restore" in sys.argv:
        restore_features()
    else:
        # Recover from an interrupted prior run before taking a fresh snapshot.
        restore_features()
        seed()
