# IsoWorks Desktop

PyQt desktop client for IsoWorks — the same lab pipelines as [isoworks-web](../isoworks-web) (SIAM, TRIMS, NGAM, AMS ¹⁴C, and more), as a native desktop app instead of a browser app.

## Setup

`isoworks_core` (data reduction engines, DB layer, parsers) is a separate, independent sibling repo shared with `isoworks-web` — clone it first:

```bash
git clone https://github.com/dagnachewl/isoworks-core.git ../isoworks-core
```

Then set up this app's environment (a conda env named `isoworks` is what the rest of this README and `IsoWorks.spec` assume, but a plain venv works too — `requirements.txt` covers everything including PyQt5):

```bash
conda create -n isoworks python=3.11
conda activate isoworks
pip install -r requirements.txt
pip install -e ../isoworks-core
```

## Running

```bash
conda activate isoworks
python launcher.py
```

On first launch the Settings panel opens; enter PostgreSQL credentials and click **Test & Save Connection**, then restart.

## Building a standalone executable

See `IsoWorks.spec` (PyInstaller). Build from the project root with the same env active:

```bash
conda activate isoworks
pyinstaller IsoWorks.spec
```
