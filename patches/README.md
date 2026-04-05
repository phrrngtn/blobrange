# duckdb-python patch: `add_replacement_scan`

This directory contains a patch to duckdb-python that adds
`con.add_replacement_scan(callback)` to the Python API.

## Files

| File | Purpose |
|---|---|
| `duckdb-python-add-replacement-scan.patch` | `git apply`-able patch against duckdb-python v1.5.1 |
| `PR_DESCRIPTION.md` | Terse PR description for upstream submission |
| `DESIGN.md` | Full design narrative: problem space, rejected alternatives, implementation path, GIL safety, quoting behavior |

## Quick start

```bash
cd /path/to/duckdb-python   # checked out at v1.5.1
git apply /path/to/blobrange/patches/duckdb-python-add-replacement-scan.patch

# Install build deps
pip install "scikit-build-core>=0.11.4" "pybind11[global]>=2.6.0" "setuptools_scm>=8.0"

# Build
OVERRIDE_GIT_DESCRIBE=v1.5.1 pip install --no-build-isolation -e .
```
