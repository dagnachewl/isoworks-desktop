"""
ngam_script_generator.py
========================
Pure-Python generator for LabView Noble Gas control scripts.

Produces (all written to a user-selected folder):
  NobleControlScript_{run_id}.txt   — main LabView sequence driver
  SubSampleDegassedPort{N}.txt      — per-sample inlet queue script (one per sample port)
  PipetteSettings.txt               — reference vessel + pipette configuration
  NC_Sequence_{run_id}_{ts}.csv     — Qtegra load list

No Qt, no DB access.  All inputs come from caller (see NgScriptGenerator).
Caller is responsible for writing the returned strings to disk.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class NgInletDef:
    """One row from ngam.ngpreparations joined to sample/analysis."""
    position: int
    analysis_id: int
    port_number: Optional[int]
    lab_id: str             # prefix-sampleid, e.g. "GSF-1234"
    sample_name: str        # sname from public.sample (used as fallback for size)
    is_blank: bool
    is_repro_ref: bool
    is_lin_ref: bool
    ref_gas: str            # nvcreferencegas, e.g. "Spike Large", "Air Small"
    ref_amount: Optional[float]

    @property
    def inlet_class(self) -> str:
        """Return 'blank' | 'spike' | 'air' | 'sample'."""
        if self.is_blank:
            return "blank"
        if self.is_repro_ref or self.is_lin_ref:
            return "air" if "air" in (self.ref_gas or "").lower() else "spike"
        return "sample"

    @property
    def size(self) -> str:
        """Return 'Large' | 'Small' derived from ref_gas, then sample_name fallback."""
        rg = (self.ref_gas or "").lower()
        if "small" in rg:
            return "Small"
        if "large" in rg:
            return "Large"
        parts = (self.sample_name or "").split("_")
        if len(parts) > 3:
            return parts[3]
        return "Large"

    @property
    def branch_number(self) -> Optional[int]:
        """Ports 1-4 → branch 1; ports 5-8 → branch 2."""
        if self.port_number is None:
            return None
        return 1 if self.port_number <= 4 else 2

    @property
    def inlet_descriptor(self) -> str:
        """InletDescriptor variable embedded in the per-port script."""
        return f"{self.lab_id}_AID{self.analysis_id}"


@dataclass
class NgPipetteDef:
    """One row from ngam.ng_pipette."""
    name: str
    control_type: int   # 0 = pneumatic, 1 = single valve
    valve_in: int
    valve_out: int
    select_switch: int
    volume: float
    vessel_name: str
    initial_counter: float
    actual_counter: float


@dataclass
class NgVesselDef:
    """One row from ngam.ng_reference_vessel."""
    name: str
    gas_name: str
    volume: float
    tubing_volume: float
    fill_pressure: Optional[float]      # None when is_live_conditions
    fill_temperature: Optional[float]
    fill_humidity: Optional[float]
    is_live_conditions: bool = False    # True → substitute live room conditions


# ─────────────────────────────────────────────────────────────────────────────
# Defaults (match VBA legacy values; DB overrides these when populated)
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_PIPETTES: list[NgPipetteDef] = [
    NgPipetteDef("MasterAir",  0,   0,   0, 405, 0.400000, "AirMasterTank",   0.0,   0.0),
    NgPipetteDef("SpikeSmall", 1, 302, 301, 301, 0.101465, "SpikeTank",       0.0,   7.0),
    NgPipetteDef("SpikeLarge", 1, 304, 303, 303, 0.401682, "SpikeTank",       0.0, 461.0),
    NgPipetteDef("AirSmall",   1, 402, 401, 401, 0.101465, "AirLargeTank",  432.0, 454.0),
    NgPipetteDef("AirLarge",   1, 320, 318, 318, 0.524400, "AirLargeTank",    0.0, 1205.0),
]

DEFAULT_VESSELS: list[NgVesselDef] = [
    NgVesselDef("AirLargeTank",     "Air",   1_000_000.0,  0.0,  None,   None,  None,  is_live_conditions=True),
    NgVesselDef("AirMasterTank",    "Air",      5_007.18, 21.41, 1038.7, 25.0, 50.0),
    NgVesselDef("SpikeTank",        "Spike",    5_007.18, 21.41,  117.0, 23.3,  0.0),
    NgVesselDef("AirDiluted1lTank", "Air",      1_005.16,  0.0,    1.0,  15.5, 83.0),
]


# ─────────────────────────────────────────────────────────────────────────────
# Valve map  (valve_type = "E" = Extracted Samples)
# D = Diffusion Samplers not yet implemented; valve numbers TBD
# ─────────────────────────────────────────────────────────────────────────────

_VALVE_MAP_E: dict[int, int] = {
    1: 209, 2: 210, 3: 219, 4: 212,
    5: 213, 6: 214, 7: 215, 8: 216,
}


# ─────────────────────────────────────────────────────────────────────────────
# Generator
# ─────────────────────────────────────────────────────────────────────────────

class NgScriptGenerator:
    """
    Build all LabView script files for one NG sequence run.

    Parameters
    ----------
    run_id          : ngam.msrun.runid
    inlets          : ordered list of NgInletDef (by positioninrun)
    protocol_folder : absolute Windows path written into SubScript() calls,
                      e.g. r"D:\\NobleControl\\Scripts\\Helix_Ver1\\SequenceDetails"
    valve_type      : "E" (Extracted) only for now
    pipettes        : list from DB; falls back to DEFAULT_PIPETTES
    vessels         : list from DB; falls back to DEFAULT_VESSELS
    """

    def __init__(
        self,
        run_id: int,
        inlets: list[NgInletDef],
        protocol_folder: str,
        valve_type: str = "E",
        pipettes: list[NgPipetteDef] | None = None,
        vessels: list[NgVesselDef] | None = None,
    ):
        self._run_id   = run_id
        self._inlets   = inlets
        self._folder   = protocol_folder.rstrip("/\\")
        self._vtype    = valve_type
        self._pipettes = pipettes if pipettes is not None else DEFAULT_PIPETTES
        self._vessels  = vessels  if vessels  is not None else DEFAULT_VESSELS

    # ── helpers ───────────────────────────────────────────────────────────────

    def _f(self, filename: str) -> str:
        return self._folder + "\\" + filename

    def _valve(self, port: int) -> int:
        if self._vtype == "E":
            return _VALVE_MAP_E.get(port, -99)
        return -99

    def _subscript_name(self, inlet: NgInletDef) -> str:
        cls = inlet.inlet_class
        if cls == "blank":
            return "Inlet_Blank.txt"
        if cls == "spike":
            return f"StandardSpike{inlet.size}.txt"
        if cls == "air":
            return f"StandardAir{inlet.size}.txt"
        return f"{inlet.lab_id}.txt"

    def _inlet_comment(self, inlet: NgInletDef) -> str:
        label = {
            "blank":  "Blank",
            "spike":  "Spike",
            "air":    "Air",
            "sample": "Unknown",
        }[inlet.inlet_class]
        return f"<c> {inlet.position} {label} </c>"

    # ── Sequence metadata (header comment) ───────────────────────────────────

    def _metadata_comment(self) -> str:
        n_blank  = sum(1 for i in self._inlets if i.inlet_class == "blank")
        n_spike  = sum(1 for i in self._inlets if i.inlet_class == "spike")
        n_air    = sum(1 for i in self._inlets if i.inlet_class == "air")
        n_sample = sum(1 for i in self._inlets if i.inlet_class == "sample")

        samples = [i for i in self._inlets if i.inlet_class == "sample"]
        if not samples:
            branch_note = "No Unknown Samples"
        else:
            branches = {1 if (i.port_number or 0) <= 4 else 2 for i in samples if i.port_number}
            if len(branches) == 2:
                branch_note = "Both Branches will be used"
            elif 1 in branches:
                branch_note = "Branch #1 will be used"
            else:
                branch_note = "Branch #2 will be used"

        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        return (
            f"<c> This sequence contains {len(self._inlets)} inlets; "
            f"{n_blank} blanks, {n_air} air standards, "
            f"{n_spike} spikes, and {n_sample} unknowns "
            f"({now}). {branch_note}</c>"
        )

    # ── Main sequence script ──────────────────────────────────────────────────

    def generate_main_script(self) -> tuple[str, str]:
        """Return (filename, content) for NobleControlScript_{run_id}.txt."""
        f = self._f
        n_sample = sum(1 for i in self._inlets if i.inlet_class == "sample")
        ln: list[str] = [
            self._metadata_comment(),
            "<c>Created by IsoWorks NG Module</c>",
            "", "", "",
            "Queue(SequenceMaster);",
            "<c> Sequence Variable and Notifier Initialization </c>",
            f'SubScript("{f("InitializationVariables.txt")}");',
            f'SubScript("{f("InitializationNotifier.txt")}");',
            "<c> Machine Initialization </c>",
            f'SubScript("{f("InitializationTemperatures.txt")}");',
            "<c> Cryo Initialization checkup: Cryo temperature reached? </c>",
            f'SubScript("{f("InitializationWaitForCryo.txt")}");',
            "<c> Set all Valves to start state </c>",
            f'SubScript("{f("InitializationValves.txt")}");',
            "Start of Sequence and Definition of Variables needed </c>"
            "<c> Necessary for Sequence Control:",
            "Variable(PumpTemperatureAr,Set,Float,170);",
            "Variable(PumpTemperatureArChange,Set,Integer,0);",
            r'Variable(BasePath,Set,String,Computation(String,"D:\NobleControlResults\",+,SystemTimeAsString(DateOnly)));',
            r'Variable(BasePath,Set,String,Computation(String,VariableGet(BasePath),+," \ "));',
            "Variable(InSequenceNumber,Set,Integer,0);",
            "<c> Inlet from branch 1 and/or 2 and heavies are measured </c>",
            "Variable(InletBranch,Set,Integer,2);",
            "Variable(bGetterBeforeFreeze,Set,Integer,0);",
            "Variable(bMeasureArgon,Set,Integer,1);",
            "Variable(bMeasureKrXe,Set,Integer,1);",
            (
                f'Event(ProtocolFunction,"Sequence Description: Run {self._run_id}'
                f" : Sequence with {len(self._inlets)} inlets"
                f' (including {n_sample} samples), Sequence started ",'
                "SystemTimeAsString(DateOnly));"
            ),
            "",
            "<c> New Variables prepared for Helix Development  </c>",
            "Variable(SkipWTrap,Set,Integer,0);",
            "Variable(SkipHePartition,Set,Integer,0);",
            "<c> New Variables prepared for Helix Development  </c>",
            "", "",
            'Event(Protocol,"Start of Sequence Measurement",0);',
            'Event(ProtocolFunction,"Sequence Description: Sequence started ",SystemTimeAsString(DateOnly));',
            "<c> The steps of the sequence measurements are coded one script level below.",
            "Only preparation, Inlet and Measurement is defined here.",
            "This definition follows:",
            "</c>",
            "",
            "Variable(InletBranch,Set,Integer,2);",
            "",
        ]

        for inlet in self._inlets:
            ln += [
                "",
                self._inlet_comment(inlet),
                f'SubScript("{f("Inlet_Prepare.txt")}");',
                f'SubScript("{f(self._subscript_name(inlet))}");',
            ]
            if inlet.inlet_class == "sample":
                ln.append(f'SubScript("{f("Inlet_PressureSecurity.txt")}");')
            ln.append(f'SubScript("{f("Inlet_PrepareAfter.txt")}");')

        ln += [
            "",
            "<c> Wait until the last sample is ready </c>",
            "Wait(Notifier,500,InletSystemReady,0,500,0);",
            "<c> Tell the end of sequence measurements to the protocol </c> ",
            'Event(Protocol,"End of Sequence Measurement",0);',
            "<c> TemperatureSet(NGSepTrap,98);</c> ",
            "<c> Set the Temperature to the Cleaning Temperature</c> ",
            "<c>TemperatureSet(CryoCharcoal,300);</c>",
            "<c>Valve(10K_Chamber,1);</c>",
            "<c>Valve(ToSRG,1);</c>",
        ]

        return f"NobleControlScript_{self._run_id}.txt", "\n".join(ln)

    # ── Per-sample inlet scripts ──────────────────────────────────────────────

    def generate_sample_inlet_scripts(self) -> list[tuple[str, str]]:
        """Return [(filename, content), ...] for each sample port."""
        results: list[tuple[str, str]] = []
        for inlet in self._inlets:
            if inlet.inlet_class != "sample":
                continue
            port   = inlet.port_number
            branch = inlet.branch_number
            valve  = self._valve(port) if port is not None else -99

            ln: list[str] = [
                "<c> ",
                "Inlet of SubSamples always runs in Queue Inlet",
                f"This file describes Inlet of Port {port} (Valve {valve}) ",
                "</c>",
                "Queue(Inlet);",
                "",
                "<c> Set the Inlet Descriptor </c>",
                f'Variable(InletDescriptor,Set,String,"{inlet.inlet_descriptor}");',
                "Variable(MessageInlet,Set,String,ConvertToString(Integer,VariableGet(InSequenceNumber)));",
                'Variable(MessageInlet,Set,String,Computation(String,VariableGet(MessageInlet),+,": "));',
                "Variable(MessageInlet,Set,String,Computation(String,VariableGet(MessageInlet),+,VariableGet(InletDescriptor)));",
                'Event(ProtocolFunction,"Inlet Description of InSequence Number",VariableGet(MessageInlet));',
                "",
                "<c> Make sure this inlet receives a peak center during SMS measurement </c>",
                "Variable(PeakCenter,Set,Integer,1);",
                "Variable(PeakAdjust,Set,Integer,1);",
                "",
                "<c> Prepare valves in the inlet region. </c>",
                "Valve(PumpInletSystemHV,0);",
                "Valve(ToWTrap,0);",
                f"Valve(InletFromBranch{branch},1);",
                "Valve(FromInlet,1);",
                "",
                "<c> The actual Inlet </c>",
                f"Valve(InletPort{port},1);",
                "",
                "<c> And make sure this SubSample is recognized as containing much water </c>",
                "Variable(WetSubSample,Set,Integer,1);",
            ]
            results.append((f"SubSampleDegassedPort{port}.txt", "\n".join(ln)))

        return results

    # ── PipetteSettings.txt ───────────────────────────────────────────────────

    def generate_pipette_settings(
        self,
        temperature: float,
        pressure: float,
        humidity: float,
    ) -> tuple[str, str]:
        """Return ('PipetteSettings.txt', content)."""
        ln: list[str] = [
            "<Reference definition>",
            "",
            "The Pipette Settings define:",
            "the Name of the pipette (OneWord);",
            "the kind of pipette (pneumatic control=0; single valve control=1);",
            "the two valve switches (In; Out;) for single valve control (ignored in case of pneumatic control);",
            "the Select switch (for pneumatic control);",
            "the Volume; ",
            "the name of the Reference Vessel it is connected to (OneWord);",
            "the Initial counter (at the time of last Reference vessel filling);",
            "the actual counter;",
            "",
            "<Pipette Settings>",
        ]
        for p in self._pipettes:
            ln.append(
                f"{p.name};{p.control_type};{p.valve_in};{p.valve_out};"
                f"{p.select_switch};{p.vessel_name};{p.volume:.6f};"
                f"{p.initial_counter};{p.actual_counter};"
            )
        ln += [
            "</Pipette Settings>",
            "",
            "The Reference Vessel Settings define:",
            "the Name of the Vessel (OneWord);",
            "the Name of the Reference gas inside (OneWord);",
            "the Volume of the vessel; the Volume of tubings to the pipettes;",
            "the  conditions at the time of last reference vessel filling:",
            "Pressure (hPa); Temperature (deg C); relative Humidity (%);",
            "",
            "<Reference Vessel Settings>",
        ]
        for v in self._vessels:
            if v.is_live_conditions:
                p_s, t_s, h_s = str(pressure), str(temperature), str(humidity)
            else:
                p_s = str(v.fill_pressure  if v.fill_pressure  is not None else 0)
                t_s = str(v.fill_temperature if v.fill_temperature is not None else 0)
                h_s = str(v.fill_humidity  if v.fill_humidity  is not None else 0)
            ln.append(
                f"{v.name};{v.gas_name};    {v.volume}; {v.tubing_volume};"
                f" {p_s}; {t_s}; {h_s};"
            )
            ln.append("")

        ln += ["</Reference Vessel Settings>", "", "</Reference definition>"]
        return "PipetteSettings.txt", "\n".join(ln)

    # ── Qtegra load-list CSV ──────────────────────────────────────────────────

    def generate_qtegra_csv(self) -> tuple[str, str]:
        """Return (filename, content) for the Qtegra load list CSV."""
        rows = ["Name, Comment, Peak Center"]
        for inlet in self._inlets:
            cls = inlet.inlet_class
            if cls == "blank":
                name = "Blank"
            elif cls == "spike":
                # VBA: "Spike" & Left(LargeSmall,1) & Right(LargeSmall,1)
                name = f"Spike{inlet.size[0]}{inlet.size[-1]}"
            elif cls == "air":
                name = f"Air{inlet.size[0]}{inlet.size[-1]}"
            else:
                name = f"Port#{inlet.port_number}"
            rows.append(f"{name},,TRUE")

        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        return f"NC_Sequence_{self._run_id}_{ts}.csv", "\n".join(rows)

    # ── Generate all files ────────────────────────────────────────────────────

    def generate_all(
        self,
        temperature: float = 20.0,
        pressure: float = 1013.25,
        humidity: float = 50.0,
    ) -> dict[str, str]:
        """Return {filename: content} for all generated files."""
        files: dict[str, str] = {}
        for result in [
            self.generate_main_script(),
            self.generate_pipette_settings(temperature, pressure, humidity),
            self.generate_qtegra_csv(),
            *self.generate_sample_inlet_scripts(),
        ]:
            fn, content = result
            files[fn] = content
        return files
