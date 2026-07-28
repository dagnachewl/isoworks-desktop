"""
db_connector.py — Shared database connection singleton for IsoWorks.
Holds the active connection string and dialect so all modules can import
a single DBConnector instance rather than managing their own connections.
"""
# db_connector.py
# This file holds the shared database connection info
# so all windows in the application can access it.

class DBConnector:
    def __init__(self):
        self.connection_string = None
        self.db_dialect = None # 'ACCESS' or 'SQL_SERVER'

# Create a single, shared instance that all modules can import
db_connection = DBConnector()