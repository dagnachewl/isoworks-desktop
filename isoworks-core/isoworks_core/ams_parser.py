"""
ams_parser.py — AMS data file parser for IsoWorks.

Instrument-specific behaviour (column aliases, header key names, target type
codes, date formats) is driven by YAML profiles in instruments/*.yaml next to
this file.  The parser selects the profile whose 'match' patterns appear in the
MACHINE header of the data file, falling back to instruments/default.yaml, then
to the built-in HVEE Tandetron constants if PyYAML is not available.

Adding a new instrument:
  1. Copy instruments/default.yaml → instruments/my_instrument.yaml
  2. Set name, match, and override only the sections that differ.
  3. Restart the application — no Python changes needed.

Supported file layouts
──────────────────────
Aggregate mode (one row per target):

  AMSDATA
  MACHINE     3MV Tandetron
  DATE        2024-01-15
  OPERATOR    Smith J
  RUNID       R240115A
  WHEEL       W24-001

  POS  LABEL       TYPE  CYC  TIME     R14_12       ER14_12      R13_12      ER13_12      I12C    N14
    1  OXI-A       STD   10   120.5    1.20340E-12  2.300E-15    1.10230E-02 1.200E-05    3.451   12034

Per-cycle mode (CYCLE column present — one row per cycle; aggregates computed
from accepted cycles):

  POS  LABEL  TYPE  CYCLE  TIME   R14_12       ER14_12      R13_12      ER13_12      I12C   N14  REJECT
    1  OXI-A  STD     1   12.1   1.21020E-12  8.100E-15   1.10228E-02  4.200E-05   3.451  1204   0
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)

# ── Built-in defaults (used when PyYAML is absent or no YAML file matches) ────

_BUILTIN_HDR_ALIASES: dict[str, str] = {
    "runid": "run_code", "run_id": "run_code", "run": "run_code",
    "date": "run_date",
    "machine": "machine", "system": "machine", "instrument": "machine",
    "operator": "operator", "analyst": "operator",
    "wheel": "wheel_label", "wheellabel": "wheel_label",
    "magazine": "wheel_label", "wheeid": "wheel_label",
}

_BUILTIN_COL_ALIASES: dict[str, str] = {
    "pos": "pos", "position": "pos", "tgtno": "pos", "no": "pos",
    "label": "label", "name": "label", "sample": "label",
    "id": "label", "sampleid": "label",
    "type": "type", "tgttype": "type", "sampletype": "type", "cat": "type",
    "cyc": "cyc", "cycles": "cyc", "ncyc": "cyc",
    "accepted": "cyc", "naccept": "cyc",
    "cycle": "cycle", "cycno": "cycle", "cyc_no": "cycle",
    "time": "time", "runtime": "time", "rt": "time", "runtime_s": "time",
    "r14_12": "r14_12", "r14c12c": "r14_12", "ratio14": "r14_12", "r14": "r14_12",
    "er14_12": "er14_12", "err14_12": "er14_12", "sd14_12": "er14_12",
    "sigma14": "er14_12", "err14": "er14_12", "se14": "er14_12",
    "r13_12": "r13_12", "r13c12c": "r13_12", "ratio13": "r13_12", "r13": "r13_12",
    "er13_12": "er13_12", "err13_12": "er13_12", "sd13_12": "er13_12",
    "sigma13": "er13_12", "err13": "er13_12", "se13": "er13_12",
    "i12c": "i12c", "cur12c": "i12c", "current12": "i12c",
    "i12": "i12c", "beam12": "i12c",
    "n14": "n14", "cnt14": "n14", "counts14": "n14",
    "14ccts": "n14", "cts14": "n14",
    "reject": "reject", "isrejected": "reject",
    "flag": "reject", "outlier": "reject",
}

_BUILTIN_TYPE_MAP: dict[str, str] = {
    "STD": "OXI",   "OX1": "OXI",   "OXI": "OXI",   "OX-1": "OXI",
    "OX2": "OXII",  "OXII": "OXII", "OX-2": "OXII",
    "UNK": "unknown", "SAM": "unknown", "SMP": "unknown", "UNKNOWN": "unknown",
    "PBK": "process_blank",  "PBS": "process_blank",  "PROCBLK": "process_blank",
    "GBK": "graphite_blank", "GRA": "graphite_blank", "BLK": "graphite_blank",
    "GRAPHITE": "graphite_blank",
    "SST": "secondary_std",  "SEC": "secondary_std",  "SECSTD": "secondary_std",
}

_BUILTIN_DATE_FMTS  = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y%m%d")
_BUILTIN_REQ_COLS   = frozenset({"pos", "label", "type", "r14_12", "er14_12", "r13_12", "er13_12"})

# ── Instrument profile dataclass ───────────────────────────────────────────────

@dataclass
class InstrumentProfile:
    name:                 str
    match_patterns:       list[str]      # lowercase substrings matched against MACHINE header
    header_aliases:       dict[str, str]
    column_aliases:       dict[str, str]
    type_map:             dict[str, str]
    date_formats:         tuple[str, ...]
    required_columns:     frozenset[str]
    comment_char:         str  = "#"
    encoding:             str  = "utf-8"
    # "whitespace" → split() on any whitespace (default, handles tabs too)
    # "tab"        → split('\t')  — use when column headers/values may contain spaces
    # "comma"      → split(',')   — CSV-style files
    delimiter:            str  = "whitespace"
    # When True the file already contains Fm / reduced data; auto-reduce should
    # be suppressed so the import pipeline does not double-reduce.
    contains_reduced_data: bool = False


_BUILTIN_PROFILE = InstrumentProfile(
    name             = "HVEE Tandetron (built-in)",
    match_patterns   = ["tandetron", "hvee"],
    header_aliases   = _BUILTIN_HDR_ALIASES,
    column_aliases   = _BUILTIN_COL_ALIASES,
    type_map         = _BUILTIN_TYPE_MAP,
    date_formats     = _BUILTIN_DATE_FMTS,
    required_columns = _BUILTIN_REQ_COLS,
)

# ── Profile loader ─────────────────────────────────────────────────────────────

class ProfileLoader:
    """
    Scans instruments/*.yaml on first use and selects profiles by matching
    the 'match' patterns against the MACHINE header value in a data file.

    Profiles are merged on top of the built-in defaults so a YAML only needs
    to override what differs — useful for instruments that share most column
    names with the HVEE Tandetron format.
    """

    _INSTRUMENTS_DIR = Path(__file__).parent / "instruments"

    def __init__(self) -> None:
        self._profiles:  list[InstrumentProfile] = []
        self._default:   Optional[InstrumentProfile] = None
        self._loaded:    bool = False

    # ── public ────────────────────────────────────────────────────────────────

    def get_profile(self, machine_name: str) -> InstrumentProfile:
        """Return the best matching profile for *machine_name*."""
        self._ensure_loaded()
        ml = (machine_name or "").lower()
        for p in self._profiles:
            if any(pat in ml for pat in p.match_patterns):
                log.debug("Profile '%s' selected for machine '%s'", p.name, machine_name)
                return p
        if self._default is not None:
            log.debug("No profile match for '%s'; using default", machine_name)
            return self._default
        log.debug("No YAML profiles loaded; using built-in profile for '%s'", machine_name)
        return _BUILTIN_PROFILE

    def loaded_names(self) -> list[str]:
        """Return names of all loaded profiles (for diagnostics)."""
        self._ensure_loaded()
        names = [p.name for p in self._profiles]
        if self._default:
            names.append(f"{self._default.name} [default]")
        return names

    # ── internals ─────────────────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            import yaml  # type: ignore
        except ImportError:
            log.warning("PyYAML not installed — using built-in parser profiles only. "
                        "Run: pip install pyyaml")
            return
        if not self._INSTRUMENTS_DIR.is_dir():
            log.debug("instruments/ directory not found; using built-in profiles")
            return
        for yml_path in sorted(self._INSTRUMENTS_DIR.glob("*.yaml")):
            try:
                data = yaml.safe_load(yml_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    log.warning("Skipping %s — expected a YAML mapping", yml_path.name)
                    continue
                profile = self._build(data)
                if yml_path.stem == "default":
                    self._default = profile
                    log.debug("Loaded default instrument profile")
                else:
                    self._profiles.append(profile)
                    log.debug("Loaded instrument profile: %s", profile.name)
            except Exception as exc:
                log.warning("Failed to load %s: %s", yml_path.name, exc)

    @staticmethod
    def _build(data: dict) -> InstrumentProfile:
        """Build a profile, merging YAML data on top of built-in defaults."""
        # str() guards against YAML 1.1 boolean coercion (e.g. `no:` → False).
        col_aliases = dict(_BUILTIN_COL_ALIASES)
        col_aliases.update({str(k).lower(): v
                            for k, v in data.get("column_aliases", {}).items()})

        hdr_aliases = dict(_BUILTIN_HDR_ALIASES)
        hdr_aliases.update({str(k).lower(): v
                            for k, v in data.get("header_aliases", {}).items()})

        type_map = dict(_BUILTIN_TYPE_MAP)
        type_map.update({str(k).upper(): v
                         for k, v in data.get("type_map", {}).items()})

        return InstrumentProfile(
            name                  = data.get("name", "Unknown"),
            match_patterns        = [m.lower() for m in data.get("match", [])],
            header_aliases        = hdr_aliases,
            column_aliases        = col_aliases,
            type_map              = type_map,
            date_formats          = tuple(data.get("date_formats", list(_BUILTIN_DATE_FMTS))),
            required_columns      = frozenset(data.get("required_columns",
                                                        list(_BUILTIN_REQ_COLS))),
            comment_char          = data.get("comment_char", "#"),
            encoding              = data.get("encoding", "utf-8"),
            delimiter             = data.get("delimiter", "whitespace"),
            contains_reduced_data = bool(data.get("contains_reduced_data", False)),
        )


# Module-level singleton — loaded lazily on first parse call.
_profile_loader = ProfileLoader()

# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class AMSCycleRecord:
    cycle_number:   int
    ratio14_12:     float
    ratio13_12:     float
    current12c_ua:  Optional[float]
    counts14c:      Optional[int]
    runtime_s:      Optional[float]
    is_rejected:    bool = False


@dataclass
class AMSTargetRecord:
    position:       int
    label:          str
    target_type:    str                   # normalised to schema CHECK values
    n_cycles:       int
    runtime_s:      Optional[float]
    ratio14_12:     float
    err14_12:       float
    ratio13_12:     float
    err13_12:       float
    current12c_ua:  Optional[float]
    counts14c:      Optional[int]
    cycles:         List[AMSCycleRecord] = field(default_factory=list)


@dataclass
class AMSWheelRecord:
    """Parsed content of one AMS data file."""
    run_code:              str
    run_date:              Optional[date]
    machine:               str
    operator:              str
    wheel_label:           str
    source_path:           str
    profile_name:          str  = ""     # which InstrumentProfile was used
    contains_reduced_data: bool = False  # True → suppress auto-reduce in import
    targets:               List[AMSTargetRecord] = field(default_factory=list)
    extra_meta:            dict = field(default_factory=dict)


# ── Parser ─────────────────────────────────────────────────────────────────────

class HVEETandetronParser:
    """
    Parse a single AMS data file into an AMSWheelRecord.

    The instrument profile (column aliases, type codes, etc.) is selected
    automatically from the MACHINE header in the file, falling back to the
    default profile.  Pass *profile* explicitly to override auto-detection.
    """

    # ── public interface ───────────────────────────────────────────────────────

    def __init__(self, profile: Optional[InstrumentProfile] = None) -> None:
        self._explicit_profile = profile

    def parse(self, path: str | Path) -> AMSWheelRecord:
        path = Path(path)
        # Initial read with UTF-8 to extract the machine name for profile selection.
        raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

        machine_name = self._prescan_field(raw_lines,
                                           {"machine", "system", "instrument"})
        profile = self._explicit_profile or _profile_loader.get_profile(machine_name)

        # Re-read with the profile's declared encoding if it differs.
        if profile.encoding.lower().replace("-", "") != "utf8":
            raw_lines = path.read_text(encoding=profile.encoding,
                                       errors="replace").splitlines()

        meta, col_names, data_lines = self._split_sections(raw_lines, path.name, profile)
        self._validate_columns(col_names, path.name, profile)

        per_cycle = "cycle" in col_names
        targets   = (self._parse_per_cycle(col_names, data_lines, profile)
                     if per_cycle
                     else self._parse_aggregate(col_names, data_lines, profile))

        if not targets:
            raise ValueError(f"No data rows successfully parsed from '{path.name}'.")

        return AMSWheelRecord(
            run_code              = meta.get("run_code",    path.stem),
            run_date              = self._parse_date(meta.get("run_date"), profile.date_formats),
            machine               = meta.get("machine",     machine_name),
            operator              = meta.get("operator",    ""),
            wheel_label           = meta.get("wheel_label", path.stem),
            source_path           = str(path),
            profile_name          = profile.name,
            contains_reduced_data = profile.contains_reduced_data,
            targets               = targets,
            extra_meta            = {k: v for k, v in meta.items()
                                     if k not in ("run_code", "run_date", "machine",
                                                  "operator", "wheel_label")},
        )

    # ── row splitting ──────────────────────────────────────────────────────────

    @staticmethod
    def _split_row(raw: str, profile: InstrumentProfile) -> list[str]:
        """Split a data or header row using the profile's declared delimiter."""
        if profile.delimiter == "tab":
            return raw.rstrip("\r\n").split("\t")
        if profile.delimiter == "comma":
            return raw.rstrip("\r\n").split(",")
        return raw.split()   # "whitespace" — splits on any run of whitespace

    # ── section splitting ──────────────────────────────────────────────────────

    @staticmethod
    def _prescan_field(lines: list[str], field_keys: set[str]) -> str:
        """Scan the first 30 lines for a metadata key in *field_keys*."""
        for raw in lines[:30]:
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            m = re.match(r"^(\w[\w\-]*)[\s=:]+(.+)$", stripped)
            if m and m.group(1).lower() in field_keys:
                return m.group(2).strip()
        return ""

    @classmethod
    def _split_sections(cls, lines: list[str], filename: str,
                        profile: InstrumentProfile):
        """Return (meta dict, canonical col_names list, data lines list)."""
        meta:         dict[str, str] = {}
        col_names:    list[str]      = []
        col_line_idx: int            = -1
        cc = profile.comment_char

        for i, raw in enumerate(lines):
            stripped = raw.strip()
            if not stripped or stripped.startswith(cc):
                continue

            # Column header: first token resolves to "pos" via aliases.
            # Use the profile delimiter so tab-delimited headers split correctly.
            parts = cls._split_row(stripped, profile)
            first = parts[0].lower() if parts else ""
            if profile.column_aliases.get(first) == "pos":
                col_names    = [profile.column_aliases.get(t.lower().strip(), t.lower().strip())
                                for t in parts]
                col_line_idx = i
                break

            # KEY<sep>VALUE header line — regex handles space, tab, =, : separators.
            m = re.match(r"^(\w[\w\-]*)[\s=:]+(.+)$", stripped)
            if m:
                key      = profile.header_aliases.get(m.group(1).lower(),
                                                       m.group(1).lower())
                meta[key] = m.group(2).strip()

        if col_line_idx < 0:
            raise ValueError(
                f"No column header line (first column = POS) found in '{filename}'."
            )

        data_lines = [
            ln.strip() for ln in lines[col_line_idx + 1:]
            if ln.strip() and not ln.strip().startswith(cc)
        ]
        return meta, col_names, data_lines

    @staticmethod
    def _validate_columns(col_names: list[str], filename: str,
                          profile: InstrumentProfile) -> None:
        missing = profile.required_columns - set(col_names)
        if missing:
            raise ValueError(
                f"'{filename}' is missing required columns: "
                + ", ".join(sorted(missing))
            )

    # ── aggregate-mode parsing ─────────────────────────────────────────────────

    def _parse_aggregate(self, col_names: list[str], data_lines: list[str],
                         profile: InstrumentProfile) -> list[AMSTargetRecord]:
        targets: list[AMSTargetRecord] = []
        for raw in data_lines:
            parts = self._split_row(raw, profile)
            if len(parts) < len(col_names):
                log.debug("Aggregate row too short, skipped: %s", raw[:80])
                continue
            row = dict(zip(col_names, parts))
            try:
                targets.append(self._row_to_target(row, profile))
            except Exception as exc:
                log.warning("Skipping aggregate row %r: %s", raw[:80], exc)
        return targets

    # ── per-cycle-mode parsing ─────────────────────────────────────────────────

    def _parse_per_cycle(self, col_names: list[str], data_lines: list[str],
                         profile: InstrumentProfile) -> list[AMSTargetRecord]:
        from collections import defaultdict
        import statistics

        groups: dict[tuple, list[dict]] = defaultdict(list)
        for raw in data_lines:
            parts = self._split_row(raw, profile)
            if len(parts) < len(col_names):
                log.debug("Per-cycle row too short, skipped: %s", raw[:80])
                continue
            row = dict(zip(col_names, parts))
            try:
                groups[(int(row["pos"]), row["label"])].append(row)
            except (KeyError, ValueError) as exc:
                log.warning("Skipping per-cycle row %r: %s", raw[:80], exc)

        targets: list[AMSTargetRecord] = []
        for (pos, lbl), rows in groups.items():
            try:
                targets.append(self._cycles_to_target(pos, lbl, rows, profile))
            except Exception as exc:
                log.warning("Skipping target pos=%s label=%r: %s", pos, lbl, exc)

        targets.sort(key=lambda t: t.position)
        return targets

    @staticmethod
    def _cycles_to_target(pos: int, lbl: str, rows: list[dict],
                           profile: InstrumentProfile) -> AMSTargetRecord:
        import statistics
        raw_type    = rows[0].get("type", "UNK").upper().strip("#")
        target_type = profile.type_map.get(raw_type, "other")

        cycles: list[AMSCycleRecord] = []
        for row in rows:
            try:
                rejected = bool(int(float(row.get("reject", "0"))))
                cycles.append(AMSCycleRecord(
                    cycle_number  = int(float(row.get("cycle", len(cycles) + 1))),
                    ratio14_12    = float(row["r14_12"]),
                    ratio13_12    = float(row["r13_12"]),
                    current12c_ua = _opt_float(row.get("i12c")),
                    counts14c     = _opt_int(row.get("n14")),
                    runtime_s     = _opt_float(row.get("time")),
                    is_rejected   = rejected,
                ))
            except (KeyError, ValueError) as exc:
                log.debug("Skipping cycle row for %s/%s: %s", pos, lbl, exc)

        accepted = [c for c in cycles if not c.is_rejected]
        if not accepted:
            raise ValueError("No accepted cycles")

        r14_vals = [c.ratio14_12 for c in accepted]
        r13_vals = [c.ratio13_12 for c in accepted]
        mean14   = sum(r14_vals) / len(r14_vals)
        mean13   = sum(r13_vals) / len(r13_vals)
        err14    = (statistics.stdev(r14_vals) / len(r14_vals) ** 0.5
                    if len(r14_vals) > 1 else 0.0)
        err13    = (statistics.stdev(r13_vals) / len(r13_vals) ** 0.5
                    if len(r13_vals) > 1 else 0.0)

        cur_vals  = [c.current12c_ua for c in accepted if c.current12c_ua is not None]
        total_cts = sum(c.counts14c for c in accepted if c.counts14c is not None) or None
        total_rt  = sum(c.runtime_s  for c in accepted if c.runtime_s  is not None) or None

        return AMSTargetRecord(
            position      = pos,
            label         = lbl,
            target_type   = target_type,
            n_cycles      = len(accepted),
            runtime_s     = total_rt,
            ratio14_12    = mean14,
            err14_12      = err14,
            ratio13_12    = mean13,
            err13_12      = err13,
            current12c_ua = sum(cur_vals) / len(cur_vals) if cur_vals else None,
            counts14c     = total_cts,
            cycles        = cycles,
        )

    @staticmethod
    def _row_to_target(row: dict, profile: InstrumentProfile) -> AMSTargetRecord:
        raw_type    = row.get("type", "UNK").upper().strip("#")
        target_type = profile.type_map.get(raw_type, "other")

        return AMSTargetRecord(
            position      = int(row["pos"]),
            label         = row["label"],
            target_type   = target_type,
            n_cycles      = _opt_int(row.get("cyc")) or 0,
            runtime_s     = _opt_float(row.get("time")),
            ratio14_12    = float(row["r14_12"]),
            err14_12      = float(row["er14_12"]),
            ratio13_12    = float(row["r13_12"]),
            err13_12      = float(row["er13_12"]),
            current12c_ua = _opt_float(row.get("i12c")),
            counts14c     = _opt_int(row.get("n14")),
        )

    # ── helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_date(val: Optional[str],
                    date_formats: tuple[str, ...]) -> Optional[date]:
        if not val:
            return None
        for fmt in date_formats:
            try:
                return datetime.strptime(val.strip(), fmt).date()
            except ValueError:
                continue
        log.warning("Could not parse date %r — stored as NULL", val)
        return None


# ── Module-level helpers ───────────────────────────────────────────────────────

def _opt_float(v: Optional[str]) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _opt_int(v: Optional[str]) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


# ── DB import helper ───────────────────────────────────────────────────────────

def import_wheel_to_db(wheel: AMSWheelRecord, run_id: int,
                       wheel_number: int = 1) -> int:
    """
    Persist a parsed AMSWheelRecord into the database under an existing
    ams.amsrun row (run_id).  Returns the new amswheelid.

    Targets whose wheel-position already exists on this wheel are updated
    in-place (idempotent re-import).
    """
    from db_core import db_manager
    from sqlalchemy import text

    with db_manager.get_connection() as conn:
        row = conn.execute(text("""
            INSERT INTO ams.amswheel
                (amsrunid, wheelnumber, wheellabel, datafilepath,
                 machinename, operatorname)
            VALUES
                (:rid, :wnum, :wlbl, :path, :mach, :oper)
            ON CONFLICT (amsrunid, wheelnumber) DO UPDATE
                SET wheellabel   = EXCLUDED.wheellabel,
                    datafilepath = EXCLUDED.datafilepath,
                    machinename  = EXCLUDED.machinename,
                    operatorname = EXCLUDED.operatorname
            RETURNING amswheelid
        """), {
            "rid":  run_id,
            "wnum": wheel_number,
            "wlbl": wheel.wheel_label,
            "path": wheel.source_path,
            "mach": wheel.machine,
            "oper": wheel.operator,
        }).fetchone()
        wheel_id = row.amswheelid

        for t in wheel.targets:
            trow = conn.execute(text("""
                INSERT INTO ams.amstarget
                    (amswheelid, wheelposition, targetlabel, targettype,
                     ncycles, runtime_s)
                VALUES
                    (:wid, :pos, :lbl, :ttype, :ncyc, :rt)
                ON CONFLICT (amswheelid, wheelposition) DO UPDATE
                    SET targetlabel = EXCLUDED.targetlabel,
                        targettype  = EXCLUDED.targettype,
                        ncycles     = EXCLUDED.ncycles,
                        runtime_s   = EXCLUDED.runtime_s
                RETURNING amstargetid
            """), {
                "wid":   wheel_id,
                "pos":   t.position,
                "lbl":   t.label,
                "ttype": t.target_type,
                "ncyc":  t.n_cycles or None,
                "rt":    t.runtime_s,
            }).fetchone()
            target_id = trow.amstargetid

            for c in t.cycles:
                conn.execute(text("""
                    INSERT INTO ams.amsmeasurement
                        (amstargetid, cyclenumber, ratio14_12, ratio13_12,
                         current12c_ua, counts14c, runtime_s, isrejected)
                    VALUES
                        (:tid, :cyc, :r14, :r13, :i12, :n14, :rt, :rej)
                    ON CONFLICT (amstargetid, cyclenumber) DO NOTHING
                """), {
                    "tid": target_id,
                    "cyc": c.cycle_number,
                    "r14": c.ratio14_12,
                    "r13": c.ratio13_12,
                    "i12": c.current12c_ua,
                    "n14": c.counts14c,
                    "rt":  c.runtime_s,
                    "rej": c.is_rejected,
                })

            conn.execute(text("""
                INSERT INTO ams.amsresult
                    (amstargetid, rawratio14_12, rawratio14_12_err,
                     ratio13_12, ratio13_12_err)
                VALUES
                    (:tid, :r14, :e14, :r13, :e13)
                ON CONFLICT (amstargetid) DO UPDATE
                    SET rawratio14_12     = EXCLUDED.rawratio14_12,
                        rawratio14_12_err = EXCLUDED.rawratio14_12_err,
                        ratio13_12        = EXCLUDED.ratio13_12,
                        ratio13_12_err    = EXCLUDED.ratio13_12_err
            """), {
                "tid": target_id,
                "r14": t.ratio14_12,
                "e14": t.err14_12,
                "r13": t.ratio13_12,
                "e13": t.err13_12,
            })

        conn.commit()

    log.info("Imported wheel '%s' → amswheelid=%d  (%d targets, profile: %s)",
             wheel.wheel_label, wheel_id, len(wheel.targets), wheel.profile_name)
    return wheel_id
