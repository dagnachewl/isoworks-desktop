"""
db_utils.py — Database utility helpers for IsoWorks.
Provides test_connection(), which verifies the current db_core engine
is reachable by executing a simple SELECT 1 query.
"""
# db_utils.py
from db_core import db_manager
from sqlalchemy import text

def test_connection():
    """
    Attempts to connect to the DB using the engine.
    """
    try:
        with db_manager.get_connection() as conn:
            # Simple query to test connection
            conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        print(f"Connection test failed: {e}")
        return False