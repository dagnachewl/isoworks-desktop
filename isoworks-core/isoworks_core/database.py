"""
database.py — Low-level database access layer for IsoWorks using pyodbc.
Provides transaction helpers, DSN connection utilities, and data-import
functions for writing isotope results into the LIMS database.
"""
from __future__ import annotations
import pandas as pd
import pyodbc
import logging
import numpy as np
import os
import re
import datetime as dt, sys
import math

from contextlib import contextmanager
from datetime import datetime

# --- small helpers ------------------------------------------------------------

@contextmanager
def _tx(conn):
    """Begin/commit/rollback a transaction."""
    old_autocommit = conn.autocommit
    conn.autocommit = False
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = old_autocommit

def _connect_dsn(dsn_path: str) -> pyodbc.Connection:
    # Your existing DSN connection wrapper may differ; keep consistent
    # Example: 'FILEDSN=C:\\path\\to\\ISOWORKSDB_DEV.dsn;PWD=...'
    return pyodbc.connect(f"FILEDSN={dsn_path}", autocommit=False)

# MeasurableID map (align with your LIMS)
MEASURABLE_ID_MAP = {
    "dD": 2,
    "d17O": 7,
    "d18O": 8,
    "d13C": 9,      # if applicable in your schema
    "d15N": 10,     # if applicable
}

# CorrectionType map from your VBA
CORR_TYPE = {
    "linearity": 100,
    "memory": 200,
    "drift": 300,
    "normalization": 400, # 400-403 normalization families if you need them
    "zscore": 500,
}

# -------- Infer roles from standards_df when roles dict is empty or missing keys
def _infer_roles_from_standards(std):
    out = {}
    if not isinstance(std, pd.DataFrame) or std.empty:
        return out

    cols = {c.lower(): c for c in std.columns}

    # Helper to list sample_ids by a boolean flag column (1/True)
    def _ids_by_flag(flag_name):
        f = cols.get(flag_name.lower())
        sid = cols.get("sample_id")
        if not f or not sid:
            return []
        s = std.loc[std[f].astype(str).isin(["1","True","true","TRUE"])][sid]
        return [str(x).strip() for x in s.dropna().tolist()]

    # Helper to pick first single id from role_code (if present)
    def _first_by_rolecode(code):
        rc = cols.get("role_code")
        sid = cols.get("sample_id")
        if not rc or not sid:
            return None
        s = std.loc[std[rc].astype(str).str.upper().str.strip() == code.upper(), sid]
        return str(s.dropna().iloc[0]).strip() if not s.dropna().empty else None

    # Try flags first (preferred)
    lin = _ids_by_flag("is_linearity_id")
    mem = _ids_by_flag("is_memory_id")
    dft = _ids_by_flag("is_drift_id")
    cal = _ids_by_flag("is_calibration_id")
    val = _ids_by_flag("is_control_id")

    if lin: out["linearity"]   = lin[0]
    if mem: out["memory"]      = mem            # multi
    if dft: out["drift"]       = dft[0]
    if cal: out["calibration"] = cal           # multi
    if val: out["validation"]  = val           # multi OK

    # If still missing, fall back to role_code (AMLIN, IHSTD, DRIFT, CTRL)
    if "linearity" not in out:
        rc_lin = _first_by_rolecode("AMLIN")
        if rc_lin: out["linearity"] = rc_lin
    if "drift" not in out:
        rc_d = _first_by_rolecode("DRIFT")
        if rc_d: out["drift"] = rc_d
    if "calibration" not in out:
        rc_cal = std.loc[std[cols.get("role_code","role_code")].astype(str).str.upper().eq("IHSTD"), cols.get("sample_id","sample_id")] \
                    if "role_code" in cols and "sample_id" in cols else pd.Series([])
        vals = [str(x).strip() for x in rc_cal.dropna().tolist()]
        if vals: out["calibration"] = vals

    return out

# --- core writer --------------------------------------------------------------

def write_results_to_db(
    dsn_path: str,
    run_id: int,
    *,
    operator_login: str,
    data_path: str,
    measurables: list[str],
    status_code: int = 8,
    run_start=None,
    run_end=None,
    roles: dict | None = None,
    methods: dict | None = None,
    inj_df=None,
    results_df=None,
    standards_df=None,
    calibration_fits: dict | None = None,
    memory_fits: dict | None = None,
    drift_fits: dict | None = None,
    memory_factors: dict | None = None,
):
    """
    SQL Server writer (LIMS-harmonized):

    Assumes inj_df/results_df ALREADY contain the LIMS-matched PositionInRun
    (i.e., created by your LIMS enrichment step). We no longer (re)[compute] or
    rename Analysis/InstrumentAnalysisID here.

    Requires:
      - inj_df: columns PositionInRun, InjectionNo/injection_no, signal-ish column (Amount/Area/water_conc), optional AnalysisTime/timestamp
      - results_df: columns PositionInRun, per-isotope {iso}_calibrated and optional {iso}_u
    """
    from datetime import datetime as _nowdt

    roles   = roles   or {}
    methods = methods or {}

    def _as_dict(x): return x if isinstance(x, dict) else {}
    roles   = _as_dict(roles)
    methods = _as_dict(methods)

    def _norm(x): return str(x or "").strip().lower()

    SUBTYPE_MAP = {
        "linearity":  {"":0,"none":0,"off":0,"linear":1},
        "memory":     {"":0,"none":0,"off":0,
                       "one pool":1,"1 pool":1,"single pool":1,"1r":1,
                       "1-reservoir (single pool)":1,"1-reservoir":1,
                       "two pool":2,"2 pool":2,"two-pool":2,"fast/slow pool carryover":2,"fast slow pool carryover":2,
                       "skip x injections":3,"skip injections":3,"skip":3,
                       "intra exponential":4,"exponential":4,
                       "intra asymptotic":5,"asymptotic":5},
        "drift":      {"":0,"none":0,"off":0,"linear":1,"order":1,"linear order":1},
        "calibration":{"":0,"none":0,"off":0,"linear":1,"bracketed":3,"normalization":1,"normalize":1},
    }
    CORR_TYPE = {"linearity":100, "memory":200, "drift":300, "calibration":400, "validation":500}
    ROLE_ALIASES = {
        "linearity":   ("linearity","linearity_id","amlin","lin","lin_id"),
        "memory":      ("memory","memory_ids","mem","mem_ids"),
        "drift":       ("drift","drift_id"),
        "calibration": ("calibration","calibration_ids","cal","cal_ids","normalization","norm","normalization_ids","norm_ids"),
        "validation":  ("validation","validation_ids","ctrl","ctrl_ids","control"),
        "control":     ("control"),
    }

    def _subtype_for(key, method_txt): return int(SUBTYPE_MAP.get(key, {}).get(_norm(method_txt), 0))
    def _is_applied_bit(code): return 1 if (code and int(code) > 0) else 0

    def _coerce_tokens(val) -> list[str]:
        if val is None: return []
        if isinstance(val, dict): vals = list(val.values())
        elif isinstance(val, (list, tuple, set)): vals = list(val)
        else: vals = val
        out = []
        for t in vals:
            for piece in re.split(r"[;,]", str(t)):
                s = piece.strip()
                if s and _norm(s) not in ("none","null","nan","—","-"):
                    out.append(s)
        return out

    def _get_role_tokens(roles_dict: dict, key: str) -> list[str]:
        rd = {_norm(k): v for k, v in (roles_dict or {}).items()}
        for alias in ROLE_ALIASES.get(key, (key,)):
            if _norm(alias) in rd:
                return _coerce_tokens(rd[_norm(alias)])
        return _coerce_tokens(rd.get(_norm(key)))

    def _our_lab_id_for(roles_dict: dict, key: str) -> str | None:
        toks = _get_role_tokens(roles_dict, key)
        if not toks: return None
        if key in ("linearity", "memory","drift", "calibration","validation"):
            return toks[0]
        return ";".join(toks)

    def _load_measurable_map(cur) -> dict:
        try:
            cur.execute("SELECT MeasurableID, MeasurableName FROM SIAM.Measurable")
            m = {}
            for mid, name in cur.fetchall():
                nm = str(name).strip().lower()
                if "18o" in nm: m["d18o"] = int(mid)
                elif "17o" in nm: m["d17o"] = int(mid)
                elif "2h" in nm or nm == "dd" or "deuterium" in nm: m["dd"] = int(mid)
                elif "13c" in nm: m["d13c"] = int(mid)
                elif "15n" in nm: m["d15n"] = int(mid)
            if m: return m
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        return {"dd":2, "d17o":7, "d18o":8, "d13c":9, "d15n":10}

    def _pick_col(df, names):
        for c in names:
            if c in df.columns: return c
        return None

    # ------------------------------------------------------------------ #
    # PostgreSQL path — uses db_manager (SQLAlchemy) instead of pyodbc    #
    # ------------------------------------------------------------------ #
    from db_core import db_manager as _dbm
    if _dbm.dialect == "POSTGRESQL":
        from sqlalchemy import text as _sa_text
        from smart_formatting import smart_round_value as _sfmt

        def _sf(v):
            """Apply smart rounding; pass through None unchanged."""
            return _sfmt(float(v)) if v is not None and not (isinstance(v, float) and np.isnan(v)) else None

        now = _nowdt.now()
        meas_str = ";".join(measurables or [])

        # Extra helpers needed for CorrectionFit section
        def _as_tokens_pg(x):
            if x is None: return []
            if isinstance(x, str):
                x = x.strip(); return [x] if x else []
            if isinstance(x, (list, tuple, set)):
                return [str(v).strip() for v in x if str(v).strip()]
            try: return [str(v).strip() for v in list(x)]
            except Exception as e:

                logging.warning(f"Exception caught: {e}"); return []

        def _split_owner_pg(tok):
            s = (tok or "").strip()
            if "-" in s:
                a, b = s.split("-", 1)
                try: return a.strip(), int(float(b.strip()))
                except (ValueError, TypeError): return a.strip(), None
            return "", None

        def _cert_pg(owner, iso):
            try:
                if not isinstance(standards_df, pd.DataFrame) or standards_df.empty:
                    return None, None
                if "sample_id" not in standards_df.columns:
                    return None, None
                row = standards_df[standards_df["sample_id"].astype(str).str.strip() == owner]
                if row.empty: return None, None
                tv = row.get(f"{iso}_true", pd.Series([None], dtype="float64")).iloc[0]
                uv = row.get(f"{iso}_uncertainty", pd.Series([None], dtype="float64")).iloc[0]
                return (float(tv) if pd.notna(tv) else None,
                        float(uv) if pd.notna(uv) else None)
            except Exception as e:

                logging.warning(f"Exception caught: {e}"); return None, None

        roles_pg = roles or {}
        # Infer missing role keys from standards
        _need = ("linearity", "memory", "drift", "calibration", "validation")
        if (not roles_pg) or any((k not in roles_pg) or (not roles_pg.get(k)) for k in _need):
            _inferred = _infer_roles_from_standards(standards_df)
            for k in _need:
                if (k not in roles_pg) or (not roles_pg.get(k)):
                    v = _inferred.get(k)
                    if v: roles_pg[k] = v

        drifters_pg   = set(_as_tokens_pg(roles_pg.get("drift")))
        linearity_pg  = set(_as_tokens_pg(roles_pg.get("linearity")))
        calib_owns_pg = set(_as_tokens_pg(roles_pg.get("calibration")))
        mem_owns_pg   = set(_as_tokens_pg(roles_pg.get("memory")))
        z_owns_pg     = set(_as_tokens_pg(roles_pg.get("validation")))
        owners_pg     = drifters_pg | linearity_pg | calib_owns_pg | mem_owns_pg | z_owns_pg

        corr_subtypes_pg = {}

        with _dbm.get_engine().begin() as _c:
            # 1. Measurables map — scoped to the procedure assigned to this run.
            #    Joining via AnalysisProcedure_Measurable ensures we only resolve
            #    measurable IDs that belong to the run's own procedure, preventing
            #    cross-procedure name collisions (e.g. d18O_H2O vs d18O_SrCO3).
            _mmap = {}
            try:
                _proc_row = _c.execute(_sa_text(
                    "SELECT procedureid FROM siam.sianalysisrun WHERE sianalysisrunid = :rid"
                ), {"rid": int(run_id)}).fetchone()
                _proc_id = int(_proc_row[0]) if _proc_row and _proc_row[0] is not None else None

                if _proc_id is not None:
                    _mrows = _c.execute(_sa_text("""
                        SELECT a.analyteid AS measurableid, a.analytename AS measurablename
                        FROM analysisprocedure_measurable apm
                        JOIN analytes a ON a.analyteid = apm.measurableid
                        WHERE apm.procedureid = :pid
                    """), {"pid": _proc_id}).fetchall()
                else:
                    # No procedure set — fall back to full table (legacy behaviour)
                    _mrows = _c.execute(_sa_text(
                        "SELECT analyteid AS measurableid, analytename AS measurablename FROM analytes"
                    )).fetchall()

                for _mid, _mname in _mrows:
                    _nm = str(_mname).strip().lower()
                    if "18o" in _nm:                                _mmap["d18o"] = int(_mid)
                    elif "17o" in _nm:                              _mmap["d17o"] = int(_mid)
                    elif "2h" in _nm or _nm == "dd" or "deuterium" in _nm:
                                                                    _mmap["dd"]   = int(_mid)
                    elif "13c" in _nm:                              _mmap["d13c"] = int(_mid)
                    elif "15n" in _nm:                              _mmap["d15n"] = int(_mid)
                if not _mmap:
                    raise ValueError("empty after procedure-scoped lookup")
            except Exception:
                _mmap = {"dd": 2, "d17o": 7, "d18o": 8, "d13c": 9, "d15n": 10}

            # 2. Employee lookup
            _tech2 = None
            try:
                _er = _c.execute(_sa_text(
                    "SELECT employeeid FROM employee"
                    " WHERE LOWER(TRIM(systemloginname)) = LOWER(TRIM(:login))"
                ), {"login": operator_login}).fetchone()
                if _er: _tech2 = int(_er[0])
            except Exception as _e:
                logging.warning("PG employee lookup failed: %s", _e)

            # 3. Update SIAnalysisRun
            _c.execute(_sa_text("""
                UPDATE siam.sianalysisrun
                   SET runstarttime=:rs, runendtime=:re, datapath=:dp,
                       measurables=:ms, runstatus=:stat, technician2=:tech2,
                       modifdatestamp=:now, modifuserstamp=:usr
                 WHERE sianalysisrunid=:run_id
            """), {"rs": run_start, "re": run_end, "dp": data_path,
                   "ms": meas_str, "stat": int(status_code), "tech2": _tech2,
                   "now": now, "usr": operator_login, "run_id": int(run_id)})

            # 4. Corrections UPSERT (sianalysiscorrection PK: runid+corrtype)
            for _key in ("linearity", "memory", "drift", "calibration", "validation"):
                _ctype = CORR_TYPE[_key]
                _code  = _subtype_for(_key, (methods or {}).get(_key, ""))
                _olab  = _our_lab_id_for(roles_pg, _key)
                if _key in ("calibration", "memory") and _olab and _code == 0:
                    _code = 1
                _applied = bool(_code)
                _res = _c.execute(_sa_text("""
                    UPDATE siam.sianalysiscorrection
                       SET isapplied=:applied, ourlabid=:olab, correctionsubtype=:subtype
                     WHERE sianalysisrunid=:rid AND correctiontype=:ctype
                """), {"applied": _applied, "olab": _olab, "subtype": int(_code),
                       "rid": int(run_id), "ctype": int(_ctype)})
                if _res.rowcount == 0:
                    _c.execute(_sa_text("""
                        INSERT INTO siam.sianalysiscorrection
                            (sianalysisrunid, correctiontype, isapplied, ourlabid, correctionsubtype)
                        VALUES (:rid, :ctype, :applied, :olab, :subtype)
                    """), {"rid": int(run_id), "ctype": int(_ctype), "applied": _applied,
                           "olab": _olab, "subtype": int(_code)})
                corr_subtypes_pg[_key] = _code
                logging.info("PG Correction[%s] subtype=%s applied=%s olab=%r", _key, _code, _applied, _olab)

            # 5. CorrectionFit rows (per owner × isotope)
            for _owner in sorted(owners_pg):
                _pfx, _sid = _split_owner_pg(_owner)
                if not _pfx or _sid is None:
                    logging.warning("Skipping CorrectionFit for %r — can't parse as Prefix-SampleID", _owner)
                    continue
                _ulin = 1 if _owner in linearity_pg  else 0
                _ucal = 1 if _owner in calib_owns_pg else 0
                _umem = 1 if _owner in mem_owns_pg   else 0
                _udrf = 1 if _owner in drifters_pg   else 0
                _uzsc = 1 if _owner in z_owns_pg     else 0

                for _iso in (measurables or []):
                    _mid = _mmap.get(_iso.lower())
                    if _mid is None: continue

                    _dfit = (drift_fits        or {}).get(_iso, {}) or {}
                    _cfit = (calibration_fits  or {}).get(_iso, {}) or {}
                    _dslp, _dint = _sf(_dfit.get("slope")), _sf(_dfit.get("intercept"))
                    _dsu,  _diu  = _sf(_dfit.get("slope_unc")), _sf(_dfit.get("intercept_unc"))
                    _lslp, _lint = _sf(_cfit.get("slope")), _sf(_cfit.get("intercept"))
                    _lsu,  _liu  = _sf(_cfit.get("slope_unc")), _sf(_cfit.get("intercept_unc"))
                    _cv, _cu = _cert_pg(_owner, _iso)

                    _base = {"rid": int(run_id), "mid": int(_mid), "pfx": _pfx, "sid": _sid}
                    _exists = bool(_c.execute(_sa_text("""
                        SELECT 1 FROM siam.sianalysiscorrectionfit
                         WHERE sianalysisrunid=:rid AND measurableid=:mid
                           AND prefix=:pfx AND sampleid=:sid
                    """), _base).fetchone())

                    if not _exists:
                        _c.execute(_sa_text("""
                            INSERT INTO siam.sianalysiscorrectionfit
                                (sianalysisrunid, measurableid, prefix, sampleid,
                                 driftslope, driftslopeunc, driftintercept, driftinterceptunc,
                                 linslope, linslopeunc, linintercept, lininterceptunc,
                                 busedforlin, busedformem, busedfordrift, busedforzscore,
                                 busedfornorm, fcertifiedvalue, fcertifiedvalueunc)
                            VALUES (:rid, :mid, :pfx, :sid,
                                    NULL,NULL,NULL,NULL, NULL,NULL,NULL,NULL,
                                    :ulin,:umem,:udrf,:uzsc,:ucal,:cv,:cu)
                        """), {**_base, "ulin": bool(_ulin), "umem": bool(_umem),
                               "udrf": bool(_udrf), "uzsc": bool(_uzsc), "ucal": bool(_ucal),
                               "cv": _cv, "cu": _cu})

                    _c.execute(_sa_text("""
                        UPDATE siam.sianalysiscorrectionfit
                           SET busedforlin=:ulin, busedformem=:umem, busedfordrift=:udrf,
                               busedforzscore=:uzsc, busedfornorm=:ucal,
                               fcertifiedvalue=:cv, fcertifiedvalueunc=:cu
                         WHERE sianalysisrunid=:rid AND measurableid=:mid
                           AND prefix=:pfx AND sampleid=:sid
                    """), {**_base, "ulin": bool(_ulin), "umem": bool(_umem),
                           "udrf": bool(_udrf), "uzsc": bool(_uzsc), "ucal": bool(_ucal),
                           "cv": _cv, "cu": _cu})

                    if _udrf and any(v is not None for v in (_dslp, _dint, _dsu, _diu)):
                        _c.execute(_sa_text("""
                            UPDATE siam.sianalysiscorrectionfit
                               SET driftslope=:ds, driftslopeunc=:dsu,
                                   driftintercept=:di, driftinterceptunc=:diu
                             WHERE sianalysisrunid=:rid AND measurableid=:mid
                               AND prefix=:pfx AND sampleid=:sid
                        """), {**_base, "ds": _dslp, "dsu": _dsu, "di": _dint, "diu": _diu})

                    if _ulin and any(v is not None for v in (_lslp, _lint, _lsu, _liu)):
                        _c.execute(_sa_text("""
                            UPDATE siam.sianalysiscorrectionfit
                               SET linslope=:ls, linslopeunc=:lsu,
                                   linintercept=:li, lininterceptunc=:liu
                             WHERE sianalysisrunid=:rid AND measurableid=:mid
                               AND prefix=:pfx AND sampleid=:sid
                        """), {**_base, "ls": _lslp, "lsu": _lsu, "li": _lint, "liu": _liu})
                    elif not _ulin:
                        _c.execute(_sa_text("""
                            UPDATE siam.sianalysiscorrectionfit
                               SET linslope=NULL, linslopeunc=NULL,
                                   linintercept=NULL, lininterceptunc=NULL
                             WHERE sianalysisrunid=:rid AND measurableid=:mid
                               AND prefix=:pfx AND sampleid=:sid
                        """), _base)

            # 6. Purge prior run data (order matters: interim→raw, result, injection)
            _c.execute(_sa_text("""
                DELETE FROM siam.sianalysisinterimdata
                 WHERE sianalysisdataid IN (
                     SELECT rd.sianalysisdataid FROM siam.sianalysisrawdata rd
                     JOIN siam.sianalysisloadlist ll ON ll.sianalysisid = rd.sianalysisid
                     WHERE ll.sianalysisrunid = :rid
                 )
            """), {"rid": int(run_id)})
            _c.execute(_sa_text("""
                DELETE FROM siam.sianalysisrawdata
                 WHERE sianalysisid IN (
                     SELECT sianalysisid FROM siam.sianalysisloadlist WHERE sianalysisrunid=:rid
                 )
            """), {"rid": int(run_id)})
            _c.execute(_sa_text("""
                DELETE FROM siam.sianalysisresult
                 WHERE sianalysisid IN (
                     SELECT sianalysisid FROM siam.sianalysisloadlist WHERE sianalysisrunid=:rid
                 )
            """), {"rid": int(run_id)})
            _c.execute(_sa_text("""
                DELETE FROM siam.sianalysisinjectiondata
                 WHERE sianalysisid IN (
                     SELECT sianalysisid FROM siam.sianalysisloadlist WHERE sianalysisrunid=:rid
                 )
            """), {"rid": int(run_id)})

            # 7. PositionInRun → SIAnalysisID map
            _pos_rows = _c.execute(_sa_text("""
                SELECT positioninrun, sianalysisid
                  FROM siam.sianalysisloadlist WHERE sianalysisrunid=:rid
            """), {"rid": int(run_id)}).fetchall()
            pos_map_pg = {int(r[0]): int(r[1]) for r in _pos_rows
                          if r[0] is not None and r[1] is not None}

            # 8. InjectionData
            if isinstance(inj_df, pd.DataFrame) and not inj_df.empty:
                _pc  = "PositionInRun" if "PositionInRun" in inj_df.columns else None
                _ic  = _pick_col(inj_df, ["InjectionNo", "injection_no"])
                if _pc and _ic:
                    _inj_rows = []
                    for _r in inj_df.to_dict('records'):
                        _pos = _r.get(_pc)
                        if pd.isna(_pos): continue
                        _siid = pos_map_pg.get(int(_pos))
                        if _siid is None: continue
                        _ino = int(_r.get(_ic) or 0)
                        if _ino <= 0: continue
                        _sig = _r.get("water_conc", _r.get("Area", _r.get("area")))
                        _sig = _sf(_sig)
                        _at  = _r.get("AnalysisTime", _r.get("timestamp", _r.get("datetime")))
                        _fl  = str(_r.get("InstrumentFlags", _r.get("instrument_flags", "")) or "")
                        _ig  = bool(_r.get("IsIgnored", _r.get("ignore", False)))
                        _inj_rows.append({"siid": _siid, "ino": _ino, "sig": _sig,
                                          "at": _at, "fl": _fl, "ig": _ig})
                    if _inj_rows:
                        _c.execute(_sa_text("""
                            INSERT INTO siam.sianalysisinjectiondata
                                (sianalysisid, injectionno, signal, analysistime,
                                 instrumentflags, isignored)
                            VALUES (:siid, :ino, :sig, :at, :fl, :ig)
                        """), _inj_rows)

            # 9. RawData
            if isinstance(inj_df, pd.DataFrame) and not inj_df.empty:
                _pc = "PositionInRun" if "PositionInRun" in inj_df.columns else None
                _ic = _pick_col(inj_df, ["InjectionNo", "injection_no"])
                if _pc and _ic:
                    _raw_rows = []
                    for _iso in (measurables or []):
                        _mid = _mmap.get(_iso.lower())
                        if _mid is None: continue
                        _rc = _pick_col(inj_df, [f"{_iso}", f"{_iso}_raw", f"{_iso}_measured"])
                        if not _rc: continue
                        for _r in inj_df.to_dict('records'):
                            _pos = _r.get(_pc)
                            if pd.isna(_pos): continue
                            _siid = pos_map_pg.get(int(_pos))
                            if _siid is None: continue
                            _ino = int(_r.get(_ic) or 0)
                            if _ino <= 0: continue
                            _val = _r.get(_rc)
                            if pd.isna(_val): continue
                            _raw_rows.append({"siid": _siid, "mid": int(_mid),
                                              "ino": _ino, "val": _sf(_val), "unc": None})
                    if _raw_rows:
                        _c.execute(_sa_text("""
                            INSERT INTO siam.sianalysisrawdata
                                (sianalysisid, injectionno, measurableid, fvalue, fvalueunc)
                            VALUES (:siid, :ino, :mid, :val, :unc)
                        """), _raw_rows)

            # 10. Final results
            if isinstance(results_df, pd.DataFrame) and not results_df.empty:
                _pc = "PositionInRun" if "PositionInRun" in results_df.columns else None
                if _pc:
                    _res_rows = []
                    for _iso in (measurables or []):
                        _mid = _mmap.get(_iso.lower())
                        if _mid is None: continue
                        _cc = _pick_col(results_df, [f"{_iso}_calibrated"])
                        _uc = _pick_col(results_df, [f"{_iso}_u"])
                        if not _cc: continue
                        for _r in results_df.to_dict('records'):
                            _pos = _r.get(_pc)
                            if pd.isna(_pos): continue
                            _siid = pos_map_pg.get(int(_pos))
                            if _siid is None: continue
                            _val = _r.get(_cc)
                            if pd.isna(_val): continue
                            _unc = _r.get(_uc) if (_uc and _uc in results_df.columns) else None
                            _unc = 0.0 if (_unc is None or pd.isna(_unc)) else (_sf(_unc) or 0.0)
                            _res_rows.append({"siid": _siid, "mid": int(_mid),
                                              "val": _sf(_val), "unc": _unc, "now": now})
                    if _res_rows:
                        _c.execute(_sa_text("""
                            INSERT INTO siam.sianalysisresult
                                (sianalysisid, measurableid, fvalue, fvalueunc, createdatestamp)
                            VALUES (:siid, :mid, :val, :unc, :now)
                        """), _res_rows)

            # 11. Memory factors → upsert via ON CONFLICT (PK covers all 4 cols)
            if not memory_factors:
                memory_factors = {}
                for _iso, _per_id in ((memory_fits or {}).items() if isinstance(memory_fits, dict) else []):
                    _fd = None
                    if isinstance(_per_id, dict):
                        _fd = _per_id.get(0)
                        if _fd is None and _per_id:
                            _fd = next(iter(_per_id.values()), None)
                    if not _fd: continue
                    _x = _fd.get("x_fit") if _fd.get("x_fit") is not None else _fd.get("x")
                    _y = next((v for v in (_fd.get("y_fit_total"), _fd.get("y_fit"), _fd.get("pred")) if v is not None), None)
                    try:
                        import math as _math
                        _xs = [int(round(float(_xx))) for _xx in list(_x)]
                        _ys = [float(_yy) if _math.isfinite(float(_yy)) else None for _yy in list(_y)]
                        memory_factors[_iso] = {i: v for i, v in zip(_xs, _ys) if v is not None}
                    except Exception as e:

                        logging.warning(f"Exception caught: {e}")

            if memory_factors:
                _mem_rows = []
                _remark = (methods or {}).get("memory", "")
                for _iso, _inj_map in memory_factors.items():
                    _mid = _mmap.get(_iso.lower())
                    if _mid is None: continue
                    for _ino, _factor in (_inj_map or {}).items():
                        try:
                            _mem_rows.append({"rid": int(run_id), "ctype": 200,
                                              "ino": int(_ino), "mid": int(_mid),
                                              "val": _sf(_factor), "unc": _sf(_factor),
                                              "rmk": str(_remark)})
                        except Exception as e:

                            logging.warning(f"Exception caught: {e}"); continue
                if _mem_rows:
                    _c.execute(_sa_text("""
                        INSERT INTO siam.sianalysiscorrectionfitinj
                            (sianalysisrunid, correctiontype, injectionno, measurableid,
                             dblvalue, dblvalueunc, remarks)
                        VALUES (:rid, :ctype, :ino, :mid, :val, :unc, :rmk)
                        ON CONFLICT (sianalysisrunid, correctiontype, injectionno, measurableid)
                        DO UPDATE SET dblvalue    = EXCLUDED.dblvalue,
                                      dblvalueunc = EXCLUDED.dblvalueunc,
                                      remarks     = EXCLUDED.remarks
                    """), _mem_rows)

            # 12. Update loadlist status from run
            _c.execute(_sa_text("""
                UPDATE siam.sianalysisloadlist ll
                   SET status = r.runstatus
                  FROM siam.sianalysisrun r
                 WHERE ll.sianalysisrunid = r.sianalysisrunid
                   AND ll.sianalysisrunid = :rid
            """), {"rid": int(run_id)})

        # engine.begin() auto-commits on successful exit
        return {
            "run_id":       int(run_id),
            "status":       int(status_code),
            "measurables":  list(measurables or []),
            "corr_subtypes": corr_subtypes_pg,
            "saved_at":     now.isoformat(timespec="seconds"),
        }

    # ------------------------------------------------------------------ #
    # SQL Server / Access path — original pyodbc implementation           #
    # ------------------------------------------------------------------ #

    conn = pyodbc.connect(f"FILEDSN={dsn_path}", autocommit=False)
    cur  = conn.cursor()
    now  = _nowdt.now()

    try:
        # Employee lookup (schema-agnostic best effort)
        technician2 = None
        try:
            cur.execute("SELECT EmployeeID FROM Employee WHERE LOWER(LTRIM(RTRIM(SystemLoginName))) = LOWER(LTRIM(RTRIM(?)))", operator_login)
            row = cur.fetchone()
            if row: technician2 = int(row[0])
        except Exception as e:
            logging.warning(f"Employee lookup failed: {e}")

        meas_map = _load_measurable_map(cur)
        meas_str = ";".join(measurables or [])

        # Update SIAnalysisRun
        cur.execute("""
            UPDATE SIAM.SIAnalysisRun
               SET RunStartTime=?, RunEndTime=?, DataPath=?, Measurables=?,
                   RunStatus=?, Technician2=?, ModifDateStamp=?, ModifUserStamp=?
             WHERE SIAnalysisRunID=?
        """, run_start, run_end, data_path, meas_str, int(status_code),
             technician2, now, operator_login, int(run_id))

        # Merge GUI roles with standards-derived roles (prefer GUI)
        need_keys = ("linearity", "memory", "drift", "calibration", "validation")
        if (not roles) or any((k not in roles) or (not roles.get(k)) for k in need_keys):
            inferred_std = _infer_roles_from_standards(standards_df)
            for k in need_keys:
                if (k not in roles) or (not roles.get(k)):
                    v = inferred_std.get(k)
                    if v: roles[k] = v

        # Corrections UPSERT
        corr_subtypes_out = {}
        for key in ("linearity","memory","drift","calibration", "validation"):
            ctype = CORR_TYPE[key]
            method_txt = (methods or {}).get(key, "")
            code  = _subtype_for(key, method_txt)
            our_lab_id = _our_lab_id_for(roles, key)

            # If IDs present but method Off → promote to Linear (1)
            if key in ("calibration","memory") and our_lab_id and code == 0:
                code = 1

            is_applied = _is_applied_bit(code)
            cur.execute("""
                UPDATE SIAM.SIAnalysisCorrection
                   SET IsApplied=?, OurLabID=?, CorrectionSubType=?
                 WHERE SIAnalysisRunID=? AND CorrectionType=?
            """, int(is_applied), our_lab_id, int(code), int(run_id), int(ctype))
            if cur.rowcount == 0:
                cur.execute("""
                    INSERT INTO SIAM.SIAnalysisCorrection
                        (SIAnalysisRunID, CorrectionType, IsApplied, OurLabID, CorrectionSubType)
                    VALUES (?,?,?,?,?)
                """, int(run_id), int(ctype), int(is_applied), our_lab_id, int(code))

            corr_subtypes_outkey = code
            logging.info("UPSERT Correction[%s] -> subtype=%s, applied=%s, OurLabID=%r", key, code, is_applied, our_lab_id)

        # --- CorrectionFit rows (per role-owner × isotope) --------------------
        # NOTE: Minimal & explicit. We trust the shapes of `drift_fitsiso` and `calibration_fitsiso`
        #       to already be flat dicts with keys: slope, intercept, slope_unc, intercept_unc.

        def _as_tokens(x):
            if x is None:
                return []
            if isinstance(x, str):
                x = x.strip()
                return [x] if x else []
            if isinstance(x, (list, tuple, set)):
                return [str(v).strip() for v in x if str(v).strip()]
            try:
                return [str(v).strip() for v in list(x)]
            except Exception as e:

                logging.warning(f"Exception caught: {e}"); return []

        def _split_owner(tok: str) -> tuple[str, int | None]:
            """Split 'J-1234' into ('J', 1234). Returns (pfx, None) if SampleID can't parse."""
            s = (tok or "").strip()
            if "-" in s:
                a, b = s.split("-", 1)
                pfx = a.strip()
                try:
                    sid = int(float(b.strip()))
                except (ValueError, TypeError):
                    sid = None
                return pfx, sid
            return "", None

        roles = roles or {}
        drifters   = set(_as_tokens(roles.get("drift")))
        linearity  = set(_as_tokens(roles.get("linearity")))
        calib_owns = set(_as_tokens(roles.get("calibration")))
        mem_owns   = set(_as_tokens(roles.get("memory")))
        z_owns     = set(_as_tokens(roles.get("validation")))

        owners = drifters | linearity | calib_owns | mem_owns | z_owns

        # Optional certified values from standards (wide): {iso}_true / {iso}_uncertainty
        def _cert(owner: str, iso: str):
            try:
                if not isinstance(standards_df, pd.DataFrame) or standards_df.empty:
                    return None, None
                if "sample_id" not in standards_df.columns:
                    return None, None
                row = standards_df[standards_df["sample_id"].astype(str).str.strip() == owner]
                if row.empty:
                    return None, None
                tv = row.get(f"{iso}_true", pd.Series([None], dtype="float64")).iloc[0]
                uv = row.get(f"{iso}_uncertainty", pd.Series([None], dtype="float64")).iloc[0]
                tv = float(tv) if pd.notna(tv) else None
                uv = float(uv) if pd.notna(uv) else None
                return tv, uv
            except Exception as e:

                logging.warning(f"Exception caught: {e}"); return None, None

        for owner in sorted(owners):
            pfx, sid = _split_owner(owner)
            if not pfx or sid is None:
                logging.warning("Skipping CorrectionFit for owner %r — could not parse as Prefix-SampleID", owner)
                continue
            used_lin = 1 if owner in linearity else 0
            used_cal = 1 if owner in calib_owns else 0
            used_mem = 1 if owner in mem_owns else 0
            used_drf = 1 if owner in drifters else 0
            used_zsc = 1 if owner in z_owns else 0

            for iso in (measurables or []):
                mid = meas_map.get(iso.lower())
                if mid is None:
                    continue

                # Pull fits plainly; if absent, leave None (we won't write them)
                dfit = (drift_fits or {}).get(iso, {}) or {}
                cfit = (calibration_fits or {}).get(iso, {}) or {}

                dslope = dfit.get("slope")
                dinter = dfit.get("intercept")
                ds_u   = dfit.get("slope_unc")
                di_u   = dfit.get("intercept_unc")

                lslope = cfit.get("slope")
                linter = cfit.get("intercept")
                ls_u   = cfit.get("slope_unc")
                li_u   = cfit.get("intercept_unc")

                cert_val, cert_unc = _cert(owner, iso)

                # Ensure row exists (keyed by run, iso(measurable), prefix, sample)
                cur.execute("""
                    SELECT 1 FROM SIAM.SIAnalysisCorrectionFit
                     WHERE SIAnalysisRunID=? AND MeasurableID=? AND Prefix=? AND SampleID=?
                """, int(run_id), int(mid), pfx, sid)
                exists = bool(cur.fetchone())

                if not exists:
                    cur.execute("""
                        INSERT INTO SIAM.SIAnalysisCorrectionFit
                          (SIAnalysisRunID, MeasurableID, Prefix, SampleID,
                           DriftSlope,DriftSlopeUnc,DriftIntercept,DriftInterceptUnc,
                           LinSlope,LinSlopeUnc,LinIntercept,LinInterceptUnc,
                           bUsedForlin,bUsedForMem,bUsedForDrift,bUsedForZScore,bUsedForNorm,
                           fCertifiedValue,fCertifiedValueUnc)
                        VALUES (?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?)
                    """, int(run_id), int(mid), pfx, sid,
                         None, None, None, None,
                         None, None, None, None,
                         used_lin, used_mem, used_drf, used_zsc, used_cal,
                         cert_val, cert_unc
                    )

                # Always refresh flags + certified values (simple update)
                cur.execute("""
                    UPDATE SIAM.SIAnalysisCorrectionFit
                       SET bUsedForlin=?,
                           bUsedForMem=?,
                           bUsedForDrift=?,
                           bUsedForZScore=?,
                           bUsedForNorm=?,
                           fCertifiedValue=?,
                           fCertifiedValueUnc=?
                     WHERE SIAnalysisRunID=? AND MeasurableID=? AND Prefix=? AND SampleID=?
                """, used_lin, used_mem, used_drf, used_zsc, used_cal,
                     cert_val, cert_unc,
                     int(run_id), int(mid), pfx, sid)

                # If this owner is a DRIFT owner and we have numbers, write ONLY drift cols
                if used_drf and any(v is not None for v in (dslope, dinter, ds_u, di_u)):
                    cur.execute("""
                        UPDATE SIAM.SIAnalysisCorrectionFit
                           SET DriftSlope=?,
                               DriftSlopeUnc=?,
                               DriftIntercept=?,
                               DriftInterceptUnc=?
                         WHERE SIAnalysisRunID=? AND MeasurableID=? AND Prefix=? AND SampleID=?
                    """, dslope, ds_u, dinter, di_u,
                         int(run_id), int(mid), pfx, sid)

                # If this owner is a LINEARITY owner (only), write linearity cols.
                # Do NOT write linearity cols for mere CALIBRATION owners.
                if used_lin and any(v is not None for v in (lslope, linter, ls_u, li_u)):
                    cur.execute("""
                        UPDATE SIAM.SIAnalysisCorrectionFit
                           SET LinSlope=?,
                               LinSlopeUnc=?,
                               LinIntercept=?,
                               LinInterceptUnc=?
                         WHERE SIAnalysisRunID=? AND MeasurableID=? AND Prefix=? AND SampleID=?
                    """, lslope, ls_u, linter, li_u,
                         int(run_id), int(mid), pfx, sid)
                elif not used_lin:
                    # Optional hygiene: make sure non-linearity owners don't carry stray linearity params
                    cur.execute("""
                        UPDATE SIAM.SIAnalysisCorrectionFit
                           SET LinSlope=NULL,
                               LinSlopeUnc=NULL,
                               LinIntercept=NULL,
                               LinInterceptUnc=NULL
                         WHERE SIAnalysisRunID=? AND MeasurableID=? AND Prefix=? AND SampleID=?
                    """, int(run_id), int(mid), pfx, sid)

        # Purge prior run data (join deletes)
        cur.execute("""
            DELETE SD
              FROM SIAM.SIAnalysisInterimData SD
              INNER JOIN SIAM.SIAnalysisRawData SRD ON SD.SIAnalysisDataID=SRD.SIAnalysisDataID
              INNER JOIN SIAM.SIAnalysisLoadList SL ON SL.SIAnalysisID=SRD.SIAnalysisID
             WHERE SL.SIAnalysisRunID=?
        """, int(run_id))
        cur.execute("""
            DELETE SRD
              FROM SIAM.SIAnalysisRawData SRD
              INNER JOIN SIAM.SIAnalysisLoadList SL ON SL.SIAnalysisID=SRD.SIAnalysisID
             WHERE SL.SIAnalysisRunID=?
        """, int(run_id))
        cur.execute("""
            DELETE SRT
              FROM SIAM.SIAnalysisResult SRT
              INNER JOIN SIAM.SIAnalysisLoadList SL ON SL.SIAnalysisID=SRT.SIAnalysisID
             WHERE SL.SIAnalysisRunID=?
        """, int(run_id))
        cur.execute("""
            DELETE SMD
              FROM SIAM.SIAnalysisInjectionData SMD
              INNER JOIN SIAM.SIAnalysisLoadList SL ON SL.SIAnalysisID=SMD.SIAnalysisID
             WHERE SL.SIAnalysisRunID=?
        """, int(run_id))

        # PositionInRun → SIAnalysisID map (from LIMS)
        cur.execute("""
            SELECT PositionInRun, SIAnalysisID
              FROM SIAM.SIAnalysisLoadList
             WHERE SIAnalysisRunID=?
        """, int(run_id))
        pos_map = {int(pos): int(siid) for (pos, siid) in cur.fetchall()
                   if pos is not None and siid is not None}
        
        # InjectionData (expects PositionInRun present)
        if isinstance(inj_df, pd.DataFrame) and not inj_df.empty:
            pos_col = "PositionInRun" if "PositionInRun" in inj_df.columns else None
            inj_col = _pick_col(inj_df, ["InjectionNo","injection_no"])
            if pos_col and inj_col:
                rows = []
                for r in inj_df.to_dict('records'):
                    pos = r.get(pos_col)
                    if pd.isna(pos): continue
                    siid = pos_map.get(int(pos))
                    if siid is None: continue
                    inj_no = int(r.get(inj_col) or 0)
                    if inj_no <= 0: continue
                    sig = r.get("water_conc", r.get("Area", r.get("area")))
                    sig = None if (sig is None or (isinstance(sig, float) and np.isnan(sig))) else float(sig)
                    atime  = r.get("AnalysisTime", r.get("timestamp", r.get("datetime")))
                    flags  = r.get("InstrumentFlags", r.get("instrument_flags",""))
                    flags  = "" if flags is None else str(flags)
                    ignore = 1 if bool(r.get("IsIgnored", r.get("ignore", False))) else 0
                    rows.append((siid, inj_no, sig, atime, flags, ignore))
                if rows:
                    cur.fast_executemany = True
                    cur.executemany("""
                        INSERT INTO SIAM.SIAnalysisInjectionData
                            (SIAnalysisID, InjectionNo, Signal, AnalysisTime, InstrumentFlags, IsIgnored)
                        VALUES (?,?,?,?,?,?)
                    """, rows)

        # RawData (per-injection measured)
        if isinstance(inj_df, pd.DataFrame) and not inj_df.empty:
            pos_col = "PositionInRun" if "PositionInRun" in inj_df.columns else None
            inj_col = _pick_col(inj_df, ["InjectionNo","injection_no"])
            if pos_col and inj_col:
                raw_rows = []
                for iso in (measurables or []):
                    mid = meas_map.get(iso.lower())
                    if mid is None: continue
                    raw_col = _pick_col(inj_df, [f"{iso}", f"{iso}_raw", f"{iso}_measured"])
                    if not raw_col: continue
                    for r in inj_df.to_dict('records'):
                        pos = r.get(pos_col)
                        if pd.isna(pos): continue
                        siid = pos_map.get(int(pos))
                        if siid is None: continue
                        inj = int(r.get(inj_col) or 0)
                        if inj <= 0: continue
                        val = r.get(raw_col)
                        if pd.isna(val): continue
                        raw_rows.append((siid, int(mid), inj, float(val), None))
                if raw_rows:
                    cur.fast_executemany = True
                    cur.executemany("""
                        INSERT INTO SIAM.SIAnalysisRawData
                            (SIAnalysisID, MeasurableID, InjectionNo, fValue, fValueUnc)
                        VALUES (?,?,?,?,?)
                    """, raw_rows)

        # try:
        #     # Use the same DSN/run/measurables, and the injection-level DataFrame you passed to write_results_to_db
        #     saved_n = save_interim_corrections(dsn_path, run_id, results_df, measurables)
        #     logging.info("Interim data rows inserted: %s", saved_n)
        # except Exception as e:
        #     logging.warning("Interim data insert skipped: %s", e)
            
        # Final results (per analysis)
        if isinstance(results_df, pd.DataFrame) and not results_df.empty:
            pos_col = "PositionInRun" if "PositionInRun" in results_df.columns else None
            if pos_col:
                final_rows = []
                for iso in (measurables or []):
                    mid = meas_map.get(iso.lower())
                    if mid is None: continue
                    cal_col = _pick_col(results_df, [f"{iso}_calibrated"])
                    u_col   = _pick_col(results_df, [f"{iso}_u"])
                    if not cal_col: continue
                    for r in results_df.to_dict('records'):
                        pos = r.get(pos_col)
                        if pd.isna(pos): continue
                        siid = pos_map.get(int(pos))
                        if siid is None: continue
                        val = r.get(cal_col)
                        if pd.isna(val): continue
                        unc = r.get(u_col) if (u_col and u_col in results_df.columns) else None
                        final_rows.append((siid, int(mid), float(val), (None if pd.isna(unc) else float(unc)), now))
                if final_rows:
                    cur.fast_executemany = True
                    cur.executemany("""
                        INSERT INTO SIAM.SIAnalysisResult
                            (SIAnalysisID, MeasurableID, fValue, fValueUnc, CreateDateStamp)
                        VALUES (?,?,?,?,?)
                    """, final_rows)

        # Memory factors (build from fits if not provided) → MERGE upsert
        if not memory_factors:
            memory_factors = {}
            mf = memory_fits or {}
            for iso, per_id in (mf.items() if isinstance(mf, dict) else []):
                fd = None
                if isinstance(per_id, dict):
                    fd = per_id.get(0) or (next(iter(per_id.values())) if per_id else None)
                if not fd:
                    continue
                x = fd.get("x_fit") if fd.get("x_fit") is not None else fd.get("x")
                y = (fd.get("y_fit_total") if fd.get("y_fit_total") is not None
                     else fd.get("y_fit") if fd.get("y_fit") is not None
                     else fd.get("pred"))
                try:
                    xs = [int(round(float(xx))) for xx in list(x)]
                    ys = [float(yy) if math.isfinite(float(yy)) else None for yy in list(y)]
                    memory_factorsiso = {i: v for i, v in zip(xs, ys) if v is not None}
                except Exception as e:

                    logging.warning(f"Exception caught: {e}")

        if memory_factors:
            rows = []
            remark = (methods or {}).get("memory", "")
            for iso, inj_map in (memory_factors or {}).items():
                mid = meas_map.get(iso.lower())
                if mid is None: continue
                for inj_no, factor in (inj_map or {}).items():
                    try:
                        rows.append((int(run_id), 200, int(inj_no), int(mid),
                                     float(factor), float(factor), str(remark)))
                    except Exception as e:

                        logging.warning(f"Exception caught: {e}"); continue
            if rows:
                cur.execute("""
                    IF OBJECT_ID('tempdb..#MemFactors') IS NOT NULL DROP TABLE #MemFactors;
                    CREATE TABLE #MemFactors (
                        SIAnalysisRunID  INT           NOT NULL,
                        CorrectionType   INT           NOT NULL,
                        InjectionNo      INT           NOT NULL,
                        MeasurableID     INT           NOT NULL,
                        dblValue         FLOAT         NULL,
                        dblValueUnc      FLOAT         NULL,
                        Remarks          NVARCHAR(255) NULL
                    );
                """)
                cur.fast_executemany = True
                cur.executemany("""
                    INSERT INTO #MemFactors
                        (SIAnalysisRunID, CorrectionType, InjectionNo, MeasurableID, dblValue, dblValueUnc, Remarks)
                    VALUES (?,?,?,?,?,?,?)
                """, rows)
                cur.execute("""
                    MERGE SIAM.SIAnalysisCorrectionFitInj AS tgt
                    USING (
                        SELECT SIAnalysisRunID, CorrectionType, InjectionNo, MeasurableID, dblValue, dblValueUnc, Remarks
                        FROM #MemFactors
                    ) AS src
                    ON (tgt.SIAnalysisRunID = src.SIAnalysisRunID
                        AND tgt.CorrectionType = src.CorrectionType
                        AND tgt.InjectionNo    = src.InjectionNo
                        AND tgt.MeasurableID   = src.MeasurableID)
                    WHEN MATCHED THEN
                        UPDATE SET
                            tgt.dblValue     = src.dblValue,
                            tgt.dblValueUnc  = src.dblValueUnc,
                            tgt.Remarks      = src.Remarks
                    WHEN NOT MATCHED BY TARGET THEN
                        INSERT (SIAnalysisRunID, CorrectionType, InjectionNo, MeasurableID, dblValue, dblValueUnc, Remarks)
                        VALUES (src.SIAnalysisRunID, src.CorrectionType, src.InjectionNo, src.MeasurableID, src.dblValue, src.dblValueUnc, src.Remarks);
                """)

        # Mirror VBA: update loadlist status from run
        cur.execute("""
            UPDATE t1
               SET t1.Status = r.RunStatus
              FROM SIAM.SIAnalysisLoadList t1
              INNER JOIN SIAM.SIAnalysisRun r ON t1.SIAnalysisRunID = r.SIAnalysisRunID
             WHERE t1.SIAnalysisRunID = ?
        """, int(run_id))

        conn.commit()
        return {
            "run_id": int(run_id),
            "status": int(status_code),
            "measurables": list(measurables or []),
            "corr_subtypes": {
                k: _subtype_for(k, (methods or {}).get(k, "")) if not (_our_lab_id_for(roles,k) and k in ("calibration","memory") and _subtype_for(k,(methods or {}).get(k,""))==0)
                   else 1
                for k in ("linearity","memory","drift","calibration")
            },
            "saved_at": now.isoformat(timespec="seconds"),
        }

    except Exception:
        conn.rollback()
        raise
    finally:
        try: cur.close()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        try: conn.close()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

def save_interim_corrections(
    dsn_path: str,
    run_id: int,
    inj_df: pd.DataFrame,
    measurables: list[str] | tuple[str, ...],
) -> int:
    """
    Persist per-injection MEMORY (ValueID=200) and DRIFT (ValueID=300) corrected values
    into SIAM.SIAnalysisInterimData(SIAnalysisDataID, ValueID, fValue, fValueUnc).

    Assumptions:
      - inj_df contains LIMS-matched PositionInRun (from enrich_data_with_lims_info).
      - You already wrote SIAM.SIAnalysisRawData for this run (so SIAnalysisDataID exists).
      - Column candidates in inj_df for each isotope:
          * memory corrected: {iso}_memory_corrected / {iso}_mem_corrected / {iso}_adjusted / {iso}_adjusted_delta
          * drift  corrected: {iso}_drift_corrected / {iso}_drift_adj / {iso}_drifted
          * uncertainty:      {iso}_u (optional / shared if specific columns do not exist)
      - Injection number column: InjectionNo / injection_no

    Returns:
      Number of interim rows written (sum of memory + drift).
    """
    if not isinstance(inj_df, pd.DataFrame) or inj_df.empty:
        return 0

    def _pick_col(df, names):
        for c in names:
            if c in df.columns:
                return c
        return None

    # Open connection
    conn = pyodbc.connect(f"FILEDSN={dsn_path}", autocommit=False)
    cur  = conn.cursor()
    total_rows = 0

    try:
        # Map PositionInRun -> SIAnalysisID for this run
        cur.execute("""
            SELECT PositionInRun, SIAnalysisID
              FROM SIAM.SIAnalysisLoadList
             WHERE SIAnalysisRunID=?
        """, int(run_id))
        pos_map = {int(pos): int(siid)
                   for (pos, siid) in cur.fetchall()
                   if pos is not None and siid is not None}

        # Measurable name->id
        def _load_measurable_map():
            try:
                cur.execute("SELECT MeasurableID, MeasurableName FROM SIAM.Measurable")
                m = {}
                for mid, name in cur.fetchall():
                    nm = str(name).strip().lower()
                    if "18o" in nm: m["d18o"] = int(mid)
                    elif "17o" in nm: m["d17o"] = int(mid)
                    elif "2h" in nm or nm == "dd" or "deuterium" in nm: m["dd"] = int(mid)
                    elif "13c" in nm: m["d13c"] = int(mid)
                    elif "15n" in nm: m["d15n"] = int(mid)
                if m: return m
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
            return {"dd":2, "d17o":7, "d18o":8, "d13c":9, "d15n":10}

        meas_map = _load_measurable_map()

        # Column discovery
        pos_col = "PositionInRun" if "PositionInRun" in inj_df.columns else None
        inj_col = _pick_col(inj_df, ["InjectionNo", "injection_no"])
        if not pos_col or not inj_col:
            # Nothing to do safely
            return 0

        # Build temp rows: (SIAnalysisID, MeasurableID, InjectionNo, ValueID, fValue, fValueUnc)
        rows = []
        for iso in (measurables or []):
            iso_key = iso.lower()

            mid = meas_map.get(iso_key)
            if mid is None:
                continue

            mem_col = _pick_col(inj_df, [f"{iso}_memory_corrected", f"{iso}_mem_corrected",
                                         f"{iso}_adjusted", f"{iso}_adjusted_delta"])
            drf_col = _pick_col(inj_df, [f"{iso}_drift_corrected", f"{iso}_drift_adj", f"{iso}_drifted"])
            u_col   = _pick_col(inj_df, [f"{iso}_u"])  # optional / shared

            # Memory (ValueID = 200)
            if mem_col:
                for r in inj_df.to_dict('records'):
                    pos = r.get(pos_col)
                    if pd.isna(pos):
                        continue
                    siid = pos_map.get(int(pos))
                    if siid is None:
                        continue
                    inj_no = int(pd.to_numeric(r.get(inj_col), errors="coerce") or 0)
                    if inj_no <= 0:
                        continue
                    val = r.get(mem_col)
                    if pd.isna(val):
                        continue
                    unc = r.get(u_col) if (u_col and u_col in inj_df.columns) else None
                    rows.append((siid, int(mid), inj_no, 200, float(val), (None if pd.isna(unc) else float(unc))))

            # Drift (ValueID = 300)
            if drf_col:
                for r in inj_df.to_dict('records'):
                    pos = r.get(pos_col)
                    if pd.isna(pos):
                        continue
                    siid = pos_map.get(int(pos))
                    if siid is None:
                        continue
                    inj_no = int(pd.to_numeric(r.get(inj_col), errors="coerce") or 0)
                    if inj_no <= 0:
                        continue
                    val = r.get(drf_col)
                    if pd.isna(val):
                        continue
                    unc = r.get(u_col) if (u_col and u_col in inj_df.columns) else None
                    rows.append((siid, int(mid), inj_no, 300, float(val), (None if pd.isna(unc) else float(unc))))

        if not rows:
            return 0

        # Temp table + INSERT via join to resolve SIAnalysisDataID
        cur.execute("""
            IF OBJECT_ID('tempdb..#Interim') IS NOT NULL DROP TABLE #Interim;
            CREATE TABLE #Interim (
                SIAnalysisID   INT NOT NULL,
                MeasurableID   INT NOT NULL,
                InjectionNo    INT NOT NULL,
                ValueID        INT NOT NULL,   -- 200 memory, 300 drift
                fValue         FLOAT NULL,
                fValueUnc      FLOAT NULL
            );
        """)
        cur.fast_executemany = True
        cur.executemany("""
            INSERT INTO #Interim (SIAnalysisID, MeasurableID, InjectionNo, ValueID, fValue, fValueUnc)
            VALUES (?,?,?,?,?,?)
        """, rows)

        # Insert into InterimData using the existing SIAnalysisDataID key from RawData
        cur.execute("""
            INSERT INTO SIAM.SIAnalysisInterimData (SIAnalysisDataID, ValueID, fValue, fValueUnc)
            SELECT r.SIAnalysisDataID, t.ValueID, t.fValue, t.fValueUnc
              FROM #Interim t
              JOIN SIAM.SIAnalysisRawData r
                ON r.SIAnalysisID = t.SIAnalysisID
               AND r.MeasurableID = t.MeasurableID
               AND r.InjectionNo  = t.InjectionNo;
        """)
        total_rows = cur.rowcount
        conn.commit()
        return int(total_rows)

    except Exception:
        conn.rollback()
        raise
    finally:
        try: cur.close()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        try: conn.close()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")


def get_connection_string(dsn_path):
    """Returns a connection string. SQLAlchemy URLs are passed through as-is."""
    if dsn_path.startswith(('postgresql://', 'postgresql+', 'mssql://', 'sqlite://')):
        return dsn_path
    if not os.path.exists(dsn_path):
        raise FileNotFoundError(f"DSN file not found at path: {dsn_path}")
    return f'FILEDSN={dsn_path};'

def get_standards_from_db(conn, loadlist_df, is_sa_url=True):
    """
    Query LIMS for standards used in the current run and return a wide table with
    columns like d18O_true, d18O_uncertainty, dD_true, dD_uncertainty, etc.
    """
    from db_core import db_manager
    from sqlalchemy import text as sa_text
    logging.info("Querying standards library from LIMS for the current batch...")

    standard_ids = tuple(loadlist_df[loadlist_df['role_code'] != 'UNKWN']['sample_id'].unique())
    if not standard_ids:
        logging.warning("No standards or controls found in the LIMS loadlist for this batch.")
        return pd.DataFrame()

    sample_id_expr = db_manager.sql_concat('rc.Prefix', "'-'", 'CAST(rc.SampleID AS VARCHAR)')
    placeholders = ','.join(f':sid{i}' for i in range(len(standard_ids)))
    params = {f'sid{i}': v for i, v in enumerate(standard_ids)}
    sql = f"""
    SELECT
        {sample_id_expr}           AS sample_id,
        m.ParameterLabel           AS parameter,
        rcd.CertifiedValue         AS true,
        rcd.CertifiedValueUnc      AS uncertainty
    FROM ReferenceControl rc
    LEFT JOIN ReferenceControlData rcd ON rc.ReferenceID = rcd.ReferenceID
    LEFT JOIN Measurables m ON rcd.MeasurableID = m.MeasurableID
    WHERE {sample_id_expr} IN ({placeholders})
    """

    if is_sa_url:
        _std_rows = conn.execute(sa_text(sql), params).fetchall()
    else:
        keys = re.findall(r':([a-zA-Z_][a-zA-Z0-9_]*)', sql)
        sql_pos = re.sub(r':[a-zA-Z_][a-zA-Z0-9_]*', '?', sql)
        _std_rows = conn.execute(sql_pos, tuple(params[k] for k in keys)).fetchall()
    df = pd.DataFrame(
        [tuple(r) for r in _std_rows],
        columns=['sample_id', 'parameter', 'true', 'uncertainty']
    )
    if df.empty:
        logging.warning("LIMS returned no rows for standards.")
        return pd.DataFrame()

    # Normalize parameter (case-insensitive mapping downstream)
    df['parameter'] = df['parameter'].astype(str).str.strip()

    # Pivot both 'true' and 'uncertainty'
    pt = df.pivot_table(
        index='sample_id',
        columns='parameter',
        values=['true', 'uncertainty'],
        aggfunc='first',
        dropna=False
    )
    standards_df = pt.reset_index()

    # Flatten MultiIndex to "PARAM_true"/"PARAM_uncertainty"
    new_cols = []
    for col in standards_df.columns:
        if isinstance(col, tuple):
            level0, level1 = col
            if level0 in ('true', 'uncertainty'):
                new_cols.append(f"{level1}_{level0}")
            else:
                new_cols.append("_".join([str(x) for x in col if x]))
        else:
            new_cols.append(col)
    standards_df.columns = new_cols

    # Canonicalize parameter names (case-insensitive)
    param_map = {
        'd2h_h2o': 'dD', 'd2h': 'dD', 'dd': 'dD', 'dd_h2o': 'dD', 'δ2h': 'dD',
        'd18o_h2o': 'd18O', 'd18o': 'd18O', 'δ18o': 'd18O',
        'd17o_h2o': 'd17O', 'd17o': 'd17O', 'δ17o': 'd17O',
        'd13c': 'd13C', 'δ13c': 'd13C',
        'd15n': 'd15N', 'δ15n': 'd15N',
    }
    rename_pairs = {}
    for c in list(standards_df.columns):
        if c == 'sample_id':
            continue
        if c.endswith('_true'):
            base = c[:-5]
            canon = param_map.get(base.lower(), base)
            rename_pairs[c] = f"{canon}_true"
        elif c.endswith('_uncertainty'):
            base = c[:-12]
            canon = param_map.get(base.lower(), base)
            rename_pairs[c] = f"{canon}_uncertainty"
    if rename_pairs:
        standards_df.rename(columns=rename_pairs, inplace=True)

    # Ensure numeric dtype for *_true / *_uncertainty
    for c in standards_df.columns:
        if c.endswith('_true') or c.endswith('_uncertainty'):
            standards_df[c] = pd.to_numeric(standards_df[c], errors='coerce')

    # Add missing uncertainty cols if needed
    for iso in ['d18O', 'dD', 'd17O', 'd13C', 'd15N']:
        tcol, ucol = f"{iso}_true", f"{iso}_uncertainty"
        if tcol in standards_df.columns and ucol not in standards_df.columns:
            standards_df[ucol] = np.nan

    logging.info(f"Successfully loaded true values for {len(standards_df)} standards from LIMS.")
    return standards_df

def _most_frequent(df, col):
    """Return the value with the highest frequency in df[col], or None."""
    vc = df[col].value_counts()
    return None if vc.empty else vc.index[0]

def get_batch_data(dsn_path, run_id, prefix_whitelist=None):
    """
    Connect to LIMS and retrieve the loadlist and standards for a given run.

    prefix_whitelist:
      - None (default)  -> use ALL prefixes (works for EA/DI/laser)
      - {'W'}           -> water-only (laser)
      - {'C','N',...}   -> EA, etc.
    """
    from db_core import db_manager
    from sqlalchemy import text as sa_text

    conn_str = get_connection_string(dsn_path)
    is_sa_url = conn_str.startswith(('postgresql://', 'postgresql+', 'mssql://', 'sqlite://'))

    if is_sa_url:
        ctx = db_manager.get_engine().connect()
    else:
        ctx = pyodbc.connect(conn_str)

    def _exec(conn, sql_str, named_params: dict):
        """Execute against either a SQLAlchemy Connection or a raw pyodbc connection."""
        if is_sa_url:
            return conn.execute(sa_text(sql_str), named_params).fetchall()
        else:
            keys = re.findall(r':([a-zA-Z_][a-zA-Z0-9_]*)', sql_str)
            sql_pos = re.sub(r':[a-zA-Z_][a-zA-Z0-9_]*', '?', sql_str)
            return conn.execute(sql_pos, tuple(named_params[k] for k in keys)).fetchall()

    with ctx as conn:
        logging.info(f"Successfully connected to LIMS: {dsn_path}")

        _ll_sql = """
        SELECT
            ll.PositionInRun        AS positioninrun,
            ll.SIAnalysisRunID      AS sianalysisrunid,
            ll.AnalysisID           AS analysisid,
            s.Prefix                AS prefix,
            s.SampleID              AS sample_id_num,
            st.strShortDescription  AS role_code,
            s.sName                 AS sample_name
        FROM
            SIAM.SIAnalysisLoadlist ll
        JOIN
            Analysis a ON ll.AnalysisID = a.AnalysisID
        JOIN
            Sample s ON a.SampleID = s.SampleID AND a.Prefix = s.Prefix
        JOIN
            tblSampleType st ON s.SampleType = st.intSampleType
        WHERE
            ll.SIAnalysisRunID = :run_id
        ORDER BY
            ll.PositionInRun
        """
        _ll_rows = _exec(conn, _ll_sql, {'run_id': run_id})
        loadlist_df = pd.DataFrame(
            [tuple(r) for r in _ll_rows],
            columns=['positioninrun', 'sianalysisrunid', 'analysisid',
                     'prefix', 'sample_id_num', 'role_code', 'sample_name']
        )
        if loadlist_df.empty:
            raise ValueError(f"No loadlist found in LIMS for SIAnalysisRunID = {run_id}")

        loadlist_df['sample_id'] = loadlist_df['prefix'] + '-' + loadlist_df['sample_id_num'].apply(lambda x: str(int(float(x))) if pd.notna(x) else '')
        # Normalize role_code
        loadlist_df['role_code'] = loadlist_df['role_code'].astype(str).str.strip()

        standards_df = get_standards_from_db(conn, loadlist_df, is_sa_url=is_sa_url)

        # Optional: restrict by prefix (laser runs commonly 'W')
        if prefix_whitelist:
            ll_filtered = loadlist_df[loadlist_df['prefix'].isin(prefix_whitelist)].copy()
        else:
            ll_filtered = loadlist_df.copy()

        # Start final standards list from loadlist presence and inner-join true values
        final_standards_df = pd.DataFrame(ll_filtered['sample_id'].unique(), columns=['sample_id'])
        final_standards_df = final_standards_df.merge(standards_df, on='sample_id', how='inner')

        # -- Initialize role flag columns ------------------------------------------
        lims_role_map = {
            'DRFTC': 'is_drift_id',
            'AMLIN': 'is_linearity_id',
            'SCTRL': 'is_control_id',
            'IHSTD': 'is_calibration_id',
            'MEM':   'is_memory_id',
            'DIWSH': 'is_diwsh_id',
        }
        for col_name in lims_role_map.values():
            final_standards_df[col_name] = 0

        # Step 1 - direct LIMS role_code assignment (authoritative when present)
        for code, col_name in lims_role_map.items():
            role_ids = ll_filtered[ll_filtered['role_code'] == code]['sample_id'].unique()
            if len(role_ids) == 0:
                continue
            if len(role_ids) == 1:
                final_standards_df.loc[
                    final_standards_df['sample_id'] == role_ids[0], col_name] = 1
                logging.info("Assigned %s to %s from role_code %s.", role_ids[0], col_name, code)
            else:
                final_standards_df.loc[
                    final_standards_df['sample_id'].isin(role_ids), col_name] = 1
                logging.info("Assigned %d samples to %s from role_code %s: %s",
                             len(role_ids), col_name, code, list(role_ids))

        # -- MEMORY: LARGEST |Delta_d18O_true| consecutive pair -------------------
        # Rule: highest delta differences, measured consecutively, beginning of batch.
        if 'd18O_true' in final_standards_df.columns and not final_standards_df['d18O_true'].isna().all():
            tmp_cert = final_standards_df.dropna(subset=['d18O_true'])
            if len(tmp_cert) >= 2:
                ll_sorted = ll_filtered.sort_values('positioninrun').copy()
                ll_sorted['prev_id'] = ll_sorted['sample_id'].shift(1)
                ll_sorted['next_id'] = ll_sorted['sample_id'].shift(-1)

                cert_ids = set(tmp_cert['sample_id'].values)
                pairs = []
                seen_pairs = set()
                for row in ll_sorted.to_dict('records'):
                    for a_col, b_col in (('prev_id', 'sample_id'), ('sample_id', 'next_id')):
                        a_id = row.get(a_col)
                        b_id = row.get(b_col)
                        if pd.isna(a_id) or pd.isna(b_id):
                            continue
                        if a_id not in cert_ids or b_id not in cert_ids:
                            continue
                        pair_key = tuple(sorted([a_id, b_id]))
                        if pair_key in seen_pairs:
                            continue
                        seen_pairs.add(pair_key)
                        a_v = float(tmp_cert.loc[tmp_cert['sample_id'] == a_id, 'd18O_true'].iloc[0])
                        b_v = float(tmp_cert.loc[tmp_cert['sample_id'] == b_id, 'd18O_true'].iloc[0])
                        diff = abs(a_v - b_v)
                        pairs.append({'sample1': a_id, 'sample2': b_id,
                                      'diff': diff, 'pos': int(row['positioninrun'])})

                if pairs:
                    # Largest diff; tie-break = earliest position (start of batch)
                    best_pair = max(pairs, key=lambda x: (x['diff'], -x['pos']))
                    hi, lo = best_pair['sample1'], best_pair['sample2']
                    final_standards_df['is_memory_id'] = 0
                    final_standards_df.loc[
                        final_standards_df['sample_id'].isin([hi, lo]), 'is_memory_id'] = 1
                    logging.info(
                        "Selected memory pair: %s / %s  (Delta_d18O_true = %.2f permil, pos = %s).",
                        hi, lo, best_pair['diff'], best_pair['pos'])
                else:
                    logging.warning(
                        "No consecutive standard pairs found for memory correction "
                        "(need >= 2 standards with certified d18O_true values).")
            else:
                logging.warning("Fewer than 2 standards with valid d18O_true for memory selection.")

        # -- CALIBRATION: widest certified-value bracket (high + low anchor) ------
        # Rule: high and low std samples measured at beginning and through the run.
        if 'd18O_true' in final_standards_df.columns:
            cert_stds = final_standards_df.dropna(subset=['d18O_true'])
            if len(cert_stds) >= 2:
                hi_id = cert_stds.loc[cert_stds['d18O_true'].idxmax(), 'sample_id']
                lo_id = cert_stds.loc[cert_stds['d18O_true'].idxmin(), 'sample_id']
                cal_ids = list(dict.fromkeys([hi_id, lo_id]))
                final_standards_df['is_calibration_id'] = 0
                final_standards_df.loc[
                    final_standards_df['sample_id'].isin(cal_ids), 'is_calibration_id'] = 1
                span = float(cert_stds['d18O_true'].max() - cert_stds['d18O_true'].min())
                logging.info(
                    "Calibration anchors: %s (high) / %s (low)  span = %.2f permil.",
                    hi_id, lo_id, span)
                if span < 5.0:
                    logging.warning(
                        "Calibration span is only %.2f permil (< 5 permil recommended). "
                        "Consider adding a standard with a wider certified value.", span)
            elif final_standards_df['is_memory_id'].sum() >= 2:
                mem_ids = final_standards_df.loc[
                    final_standards_df['is_memory_id'] == 1, 'sample_id'].tolist()
                final_standards_df.loc[
                    final_standards_df['sample_id'].isin(mem_ids), 'is_calibration_id'] = 1
                logging.warning(
                    "No certified d18O_true for explicit cal selection; "
                    "using memory pair %s as calibration anchors.", mem_ids)

        # -- DRIFT: best run-spanning IHSTD (>=2 occurrences, widest span) --------
        # Rule: spans the entire analysis time with good distribution.
        if final_standards_df['is_drift_id'].sum() != 1:
            final_standards_df['is_drift_id'] = 0

            ihstd_ids = ll_filtered[
                ll_filtered['role_code'].isin(['DRFTC', 'IHSTD'])
            ]['sample_id'].unique()

            best_sid   = None
            best_score = -1.0
            total_span = (ll_filtered['positioninrun'].max()
                          - ll_filtered['positioninrun'].min()) or 1

            for sid in ihstd_ids:
                positions = ll_filtered.loc[
                    ll_filtered['sample_id'] == sid, 'positioninrun'
                ].sort_values().values
                if len(positions) < 2:
                    continue
                span_frac    = (positions[-1] - positions[0]) / total_span
                gaps         = np.diff(positions).astype(float)
                max_gap_frac = gaps.max() / total_span
                score        = span_frac - 0.5 * max_gap_frac
                if score > best_score:
                    best_score = score
                    best_sid   = sid

            if best_sid:
                final_standards_df.loc[
                    final_standards_df['sample_id'] == best_sid, 'is_drift_id'] = 1
                logging.info(
                    "Auto-selected %s as drift standard (span score = %.2f, %d occurrences).",
                    best_sid, best_score,
                    int((ll_filtered['sample_id'] == best_sid).sum()))
            else:
                mf = _most_frequent(ll_filtered, 'sample_id')
                if mf:
                    final_standards_df.loc[
                        final_standards_df['sample_id'] == mf, 'is_drift_id'] = 1
                    logging.warning(
                        "No spanning drift candidate found; "
                        "fell back to most-frequent standard: %s.", mf)
                else:
                    logging.warning("No standards available for drift assignment.")

        # -- VALIDATION: independent control, not used for any correction ---------
        # Rule: ctrl samples preferably not used for any correction role.
        correction_sids = set(
            final_standards_df.loc[
                (final_standards_df['is_drift_id']       == 1) |
                (final_standards_df['is_calibration_id'] == 1) |
                (final_standards_df['is_memory_id']      == 1),
                'sample_id'
            ].tolist()
        )
        ctrl_role_codes = {'SCTRL', 'CTRL', 'QC', 'QCTRL', 'CONTROL', 'VAL', 'VALIDATION'}
        if final_standards_df['is_control_id'].sum() != 1:
            final_standards_df['is_control_id'] = 0

            ctrl_candidates = ll_filtered[
                ll_filtered['role_code'].isin(ctrl_role_codes) &
                ~ll_filtered['sample_id'].isin(correction_sids)
            ]
            ctrl_sid = _most_frequent(ctrl_candidates, 'sample_id') if not ctrl_candidates.empty else None

            if ctrl_sid is None:
                ctrl_candidates_any = ll_filtered[ll_filtered['role_code'].isin(ctrl_role_codes)]
                ctrl_sid = _most_frequent(ctrl_candidates_any, 'sample_id') if not ctrl_candidates_any.empty else None
                if ctrl_sid:
                    logging.warning(
                        "Control standard %s also participates in a correction role. "
                        "Consider designating an independent control.", ctrl_sid)

            if ctrl_sid is None:
                non_corr = ll_filtered[~ll_filtered['sample_id'].isin(correction_sids)]
                ctrl_sid = _most_frequent(non_corr, 'sample_id') if not non_corr.empty else None
                if ctrl_sid:
                    logging.warning(
                        "No dedicated control standard found; "
                        "using %s as fallback validation standard.", ctrl_sid)

            if ctrl_sid:
                final_standards_df.loc[
                    final_standards_df['sample_id'] == ctrl_sid, 'is_control_id'] = 1
            else:
                logging.warning("No standards available for validation (control) assignment.")

        logging.info("Successfully loaded and processed batch information from LIMS.")
        return loadlist_df, final_standards_df

def enrich_data_with_lims_info(instrument_data, loadlist_df):
    """
    LIMS-enrichment for instrument data.

    - Build run_order from (InstrumentAnalysisID, AmountRounded).
    - Rename instrument 'Analysis' -> 'InstrumentAnalysisID'.
    - Merge LIMS load list by PositionInRun.
    - Add LIMS 'AnalysisID' back as 'Analysis' in the merged output.
    - Preserve both sample IDs (instrument vs LIMS).

    Expected LIMS columns (case-insensitive): PositionInRun, sample_id, role_code, AnalysisID.
    """

    if not isinstance(instrument_data, pd.DataFrame) or instrument_data.empty:
        logging.warning("enrich_data_with_lims_info: empty instrument_data; returning as-is.")
        return instrument_data
    if not isinstance(loadlist_df, pd.DataFrame) or loadlist_df.empty:
        logging.warning("enrich_data_with_lims_info: empty loadlist_df; returning instrument_data with InstrumentAnalysisID rename.")
        df = instrument_data.copy()
        if "InstrumentAnalysisID" not in df.columns and "Analysis" in df.columns:
            df = df.rename(columns={"Analysis": "InstrumentAnalysisID"})
        return df

    df = instrument_data.copy()

    # --- 1) Normalize the instrument analysis id column name
    if "InstrumentAnalysisID" not in df.columns:
        if "Analysis" in df.columns:
            df = df.rename(columns={"Analysis": "InstrumentAnalysisID"})
        else:
            # Create a synthetic ID if missing
            df["InstrumentAnalysisID"] = "A-00001"

    # --- 2) Find an amount-like column to stabilize run_order
    amount_col = None
    for cand in ("Amount", "amount"):
        if cand in df.columns:
            amount_col = cand
            break

    # Numeric, rounded amount for grouping (or NaN if none)
    if amount_col:
        amt = pd.to_numeric(df[amount_col], errors="coerce").round(1)
    else:
        amt = pd.Series(np.nan, index=df.index)

    # --- 3) Build run_order using (InstrumentAnalysisID, AmountRounded)
    # Detect changes in the tuple from one row to the next, then cumsum
    key_now  = pd.Series(list(zip(df["InstrumentAnalysisID"].astype(str), amt.fillna(-999999))), index=df.index)
    key_prev = key_now.shift(1)
    run_order = (key_now != key_prev).astype(int).cumsum()

    df["run_order"] = run_order

    # --- 4) Normalize LIMS columns (case-insensitive lookup)
    def _col(ll, name_options):
        low = {c.lower(): c for c in ll.columns}
        for nm in name_options:
            if nm.lower() in low:
                return low[nm.lower()]
        return None

    pos_col = _col(loadlist_df, ("PositionInRun", "RunOrder", "Position", "run_order"))
    sid_col = _col(loadlist_df, ("sample_id", "SampleID", "OurLabID"))
    role_col = _col(loadlist_df, ("role_code", "RoleCode"))
    anid_col = _col(loadlist_df, ("AnalysisID", "analysisid"))
    sname_col = _col(loadlist_df, ("sample_name", "sName"))
    
    lims_info = loadlist_df.copy()
    need = [pos_col, sid_col, role_col, anid_col, sname_col]
    # Keep only the columns that exist
    lims_keep = [c for c in need if c is not None]
    if not lims_keep:
        logging.warning("enrich_data_with_lims_info: LIMS table missing expected columns; merging only on PositionInRun if present.")
        lims_keep = [c for c in (pos_col,) if c is not None]

    lims_info = lims_info[lims_keep].copy()
    # Standardize names for merge & output
    if pos_col and pos_col != "PositionInRun":
        lims_info = lims_info.rename(columns={pos_col: "PositionInRun"})
    if sid_col and sid_col != "sample_id":
        lims_info = lims_info.rename(columns={sid_col: "sample_id"})
    if role_col and role_col != "role_code":
        lims_info = lims_info.rename(columns={role_col: "role_code"})
    if anid_col and anid_col != "AnalysisID":
        lims_info = lims_info.rename(columns={anid_col: "AnalysisID"})

    # --- 5) Merge by run_order ↔ PositionInRun
    merged = pd.merge(df, lims_info, left_on="run_order", right_on="PositionInRun", how="inner", suffixes=("_instrument", "_lims"))
    
    # Preserve both instrument and LIMS sample IDs explicitly
    # If instrument_data already had sample_id, it will appear as 'sample_id_instrument' after merge (due to suffixes).
    # Ensure consistent naming:
    # if "sample_id" not in merged.columns and "sample_id_lims" in df.columns:
    merged = merged.rename(columns={"sample_id_lims": "sample_id"})
        
    # Bring LIMS AnalysisID as 'Analysis' (explicit requirement)
    if "AnalysisID" in merged.columns:
        merged["Analysis"] = merged["AnalysisID"]
    
    logging.info(
        "Successfully enriched instrument data with LIMS loadlist information. "
        f"(rows={len(merged)}, blocks={int(run_order.max())})"
    )
    return merged

def update_si_analysis_run(
    dsn_path,
    si_analysis_run_id,
    *,
    run_start_time,
    run_end_time,
    data_path,
    measurables,     # e.g., "d18O; dD"
    run_status,      # int (your iStatus)
    technician2,     # int or None
    mod_user,        # string (e.g., OS/app user)
    mod_time=None,   # optional override for ModifDateStamp
):
    """
    UPDATE SIAM.SIAnalysisRun ... WHERE SIAnalysisRunID = ?  (parameterized)

    Returns the number of affected rows (should be 1).
    """
    import datetime as _dt

    if mod_time is None:
        mod_time = _dt.datetime.now()

    sql = """
        UPDATE SIAM.SIAnalysisRun
        SET
            RunStartTime   = ?,
            RunEndTime     = ?,
            DataPath       = ?,
            Measurables    = ?,
            RunStatus      = ?,
            Technician2    = ?,
            ModifDateStamp = ?,
            ModifUserStamp = ?
        WHERE SIAnalysisRunID = ?
    """

    conn_str = get_connection_string(dsn_path)
    with pyodbc.connect(conn_str) as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                sql,
                (
                    run_start_time,
                    run_end_time,
                    data_path,
                    measurables,
                    run_status,
                    technician2,
                    mod_time,
                    mod_user,
                    si_analysis_run_id,
                ),
            )
            affected = cur.rowcount
            conn.commit()
            logging.info("SIAnalysisRun updated (id=%s, rows=%s).", si_analysis_run_id, affected)
            if affected != 1:
                logging.warning("Expected to update 1 row in SIAnalysisRun but affected=%s.", affected)
            return affected
        except Exception as e:
            conn.rollback()
            logging.error("Failed updating SIAnalysisRun id=%s: %s", si_analysis_run_id, e, exc_info=True)
            raise