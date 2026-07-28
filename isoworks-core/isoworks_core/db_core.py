"""
db_core.py — Core SQLAlchemy database manager for IsoWorks.
Provides the _DatabaseManager singleton (db_manager) with case-insensitive
row wrappers, engine creation, and connection pooling for PostgreSQL, SQL Server, and MS Access.
"""
from __future__ import annotations
import logging
import os
import urllib.parse
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

class _CIRow:
    """Wraps a SQLAlchemy Row with case-insensitive attribute and key access."""
    __slots__ = ('_m', '_v')

    def __init__(self, row):
        self._m = {k.lower(): v for k, v in row._mapping.items()}
        self._v = list(row._mapping.values())

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        return self._m.get(name.lower())

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._v[key]
        return self._m.get(str(key).lower())

    def __iter__(self):
        return iter(self._v)

    def __repr__(self):
        return f'CIRow({self._m})'


class _CIResult:
    """Wraps CursorResult to yield _CIRow objects."""

    def __init__(self, result):
        self._r = result

    def __iter__(self):
        for row in self._r:
            yield _CIRow(row)

    def fetchone(self):
        row = self._r.fetchone()
        return _CIRow(row) if row is not None else None

    def fetchall(self):
        return [_CIRow(r) for r in self._r.fetchall()]

    def first(self):
        row = self._r.first()
        return _CIRow(row) if row is not None else None

    def scalar(self):
        row = self._r.fetchone()
        if row is None:
            return None
        return row[0]

    @property
    def rowcount(self):
        return self._r.rowcount


class _CIConnection:
    """Wraps a SQLAlchemy Connection to return case-insensitive rows."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, *args, **kwargs):
        return _CIResult(self._conn.execute(*args, **kwargs))

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self._conn.__exit__(*args)


class _CIConnectionContext:
    """Context manager returned by get_connection()."""

    def __init__(self, raw_cm):
        self._cm = raw_cm

    def __enter__(self):
        return _CIConnection(self._cm.__enter__())

    def __exit__(self, *args):
        return self._cm.__exit__(*args)


class DatabaseManager:
    def __init__(self):
        self._engine: Engine = None
        self.dialect = "SQL_SERVER" # Default
        self._path_or_dsn: str = ""  # stored for subprocess handoff

    def initialize(self, dialect: str, path_or_dsn: str):
        """
        Initializes the SQLAlchemy Engine with connection pooling.
        """
        self.dialect = dialect
        self._path_or_dsn = path_or_dsn.strip()
        connection_string = ""
        
        # clean inputs
        path_or_dsn = path_or_dsn.strip()

        if dialect == "ACCESS":
            # Access connection string
            raw_conn_str = r'DRIVER={Microsoft Access Driver (*.mdb, *.accdb)};DBQ=' + path_or_dsn + ';'
            params = urllib.parse.quote_plus(raw_conn_str)
            connection_string = f"access+pyodbc:///?odbc_connect={params}"
            
        elif dialect == "SQL_SERVER":
            # SQL Server Connection Logic
            
            # 1. Determine the raw connection string
            if "FILEDSN" not in path_or_dsn.upper() and os.path.exists(path_or_dsn):
                # User passed a file path (C:/.../file.dsn)
                raw_conn_str = f"FILEDSN={path_or_dsn};"
            else:
                # User passed a raw string or DSN string
                raw_conn_str = path_or_dsn

            # 2. URL Encode the string
            # This is CRITICAL. It allows special characters (spaces, slashes) to pass to the driver safely.
            params = urllib.parse.quote_plus(raw_conn_str)
            
            # 3. Construct the SQLAlchemy URL using odbc_connect
            # This tells SQLAlchemy: "Don't try to read the file, just pass this string to pyodbc"
            connection_string = f"mssql+pyodbc:///?odbc_connect={params}"

        elif dialect == "POSTGRESQL":
            # Check if psycopg2 is available
            try:
                import psycopg2
            except ImportError:
                raise ImportError(
                    "psycopg2 module not found. Please install it with: pip install psycopg2-binary\n"
                    "Make sure you're running the application in the 'isoworks' conda environment: "
                    "conda activate isoworks && python3 launcher.py"
                )
            # PostgreSQL Connection Logic
            # path_or_dsn already includes the postgresql:// prefix from settings_module.py
            logging.info(f"Creating PostgreSQL connection with: {path_or_dsn}")
            connection_string = path_or_dsn

        logging.info(f"Initializing DB Engine ({dialect})...")
        
        try:
            # Create the engine
            # pool_pre_ping=True checks if the connection is alive before using it
            self._engine = create_engine(
                connection_string, 
                pool_size=5, 
                max_overflow=10,
                pool_pre_ping=True
            )
            logging.info("DB Engine initialized successfully.")

            # Register numpy scalar adapters for psycopg2 so numpy types are
            # transparently converted to plain Python everywhere in the codebase.
            if dialect == "POSTGRESQL":
                try:
                    import numpy as np
                    import psycopg2.extensions as _ext
                    _adapt = _ext.AsIs
                    for _np_type, _py_conv in [
                        (np.integer,  int),
                        (np.floating, float),
                        (np.bool_,    bool),
                    ]:
                        _ext.register_adapter(_np_type, lambda v, c=_py_conv: _adapt(c(v)))
                    logging.info("psycopg2 numpy adapters registered.")
                except Exception as _e:
                    logging.warning(f"Could not register numpy psycopg2 adapters: {_e}")

                # Inject the OS login name as a PostgreSQL session variable so
                # audit triggers can record app_user = current_setting('app.current_user').
                # Fires on every connection checkout from the pool.
                from sqlalchemy import event as _sa_event

                @_sa_event.listens_for(self._engine, "checkout")
                def _set_app_user(dbapi_conn, conn_record, conn_proxy):
                    try:
                        from shared_utils import get_current_user_id
                        user = get_current_user_id()
                        cur = dbapi_conn.cursor()
                        cur.execute(
                            "SELECT set_config('app.current_user', %s, false)",
                            (user,)
                        )
                        cur.close()
                    except Exception as _e:
                        logging.debug(f"Could not set app.current_user: {_e}")

                logging.info("Audit user-context injection registered.")

        except Exception as e:
            logging.error(f"Failed to create engine: {e}")
            raise

    def get_engine(self):
        if not self._engine:
            raise ConnectionError("Database not initialized. Call db_manager.initialize() first.")
        return self._engine

    def get_connection(self):
        """Returns a context-managed connection with case-insensitive row access."""
        return _CIConnectionContext(self._engine.connect())

    def sql_bool(self, value: bool) -> str:
        """Returns a dialect-appropriate boolean literal."""
        if self.dialect == "POSTGRESQL":
            return "true" if value else "false"
        return "1" if value else "0"

    def sql_top(self, n: int) -> str:
        """Returns TOP N clause for SQL Server, or empty string for PostgreSQL (use sql_limit instead)."""
        if self.dialect in ("SQL_SERVER", "ACCESS"):
            return f"TOP {n} "
        return ""

    def sql_limit(self, n: int) -> str:
        """Returns LIMIT N clause for PostgreSQL, or empty string for SQL Server (TOP used in SELECT)."""
        if self.dialect == "POSTGRESQL":
            return f" LIMIT {n}"
        return ""

    def sql_concat(self, *args) -> str:
        """Returns a dialect-appropriate string concatenation expression."""
        parts = []
        for arg in args:
            if isinstance(arg, str) and (arg.startswith("'") or arg.startswith('"')):
                parts.append(arg)
            elif self.dialect == "SQL_SERVER":
                parts.append(f"CAST({arg} AS NVARCHAR(255))")
            elif self.dialect == "POSTGRESQL":
                parts.append(f"CAST({arg} AS TEXT)")
            else:
                parts.append(str(arg))
        if self.dialect == "SQL_SERVER":
            sep = " + "
        elif self.dialect == "POSTGRESQL":
            sep = " || "
        else:
            sep = " & "
        return sep.join(parts)

# Global instance
db_manager = DatabaseManager()