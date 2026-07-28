#!/usr/bin/env Rscript
# irms/r_scripts/read_irms.R
#
# Called by irms/readers/isoverse_reader.py via subprocess.
# Reads any isoreader-supported IRMS file and writes three CSVs to <output_dir>:
#
#   file_info.csv    — per-file metadata (Analysis, Identifier 1, Method, etc.)
#   vendor_table.csv — vendor peak table joined with file-level metadata
#   raw_signals.csv  — continuous mass traces (CF) or per-cycle values (DI)
#
# Usage:
#   Rscript read_irms.R <input_file> <output_dir>
#
# Exit codes: 0 = success, 1 = error (message on stderr)

suppressPackageStartupMessages({
  library(isoreader)
  library(isoprocessor)
  library(dplyr)
  library(readr)
})

# ── Args ──────────────────────────────────────────────────────────────────────

args <- commandArgs(trailingOnly = TRUE)
if (length(args) < 2) {
  cat("Usage: Rscript read_irms.R <input_file> <output_dir>\n", file = stderr())
  quit(status = 1)
}
infile <- args[1]
outdir <- args[2]

if (!file.exists(infile)) {
  cat(sprintf("File not found: %s\n", infile), file = stderr())
  quit(status = 1)
}

dir.create(outdir, showWarnings = FALSE, recursive = TRUE)
iso_turn_info_messages_off()

# Make a data frame safe for write_csv:
#   1. Drop list/S3/non-atomic columns (iso_remove_list_columns misses some).
#   2. Strip dim attributes from 1-D arrays — readr rejects class=array even
#      when they are otherwise atomic (seen with Elementar ionOS .iarc files).
safe_df <- function(df) {
  df[] <- lapply(df, function(col) {
    if (!is.atomic(col)) return(NULL)    # drop non-atomic (list/env/S4)
    if (!is.null(dim(col))) as.vector(col) else col  # flatten arrays
  })
  df[!vapply(df, is.null, logical(1))]
}

# ── Detect IRMS type ──────────────────────────────────────────────────────────
# isoreader dispatches by extension; we mirror its lookup table:
#   Continuous Flow : .cf  .dxf  .iarc  .cf.rds
#   Dual Inlet      : .did .caf  .txt(Nu) .di.rds
#   Scan            : .scn .scan.rds

CF_EXTS   <- c(".cf", ".dxf", ".iarc")
DI_EXTS   <- c(".did", ".caf", ".txt")
SCAN_EXTS <- c(".scn")

# Handle compound extensions (.cf.rds, .di.rds, .scan.rds)
lower <- tolower(infile)
file_type <- if (any(sapply(CF_EXTS,   function(e) endsWith(lower, e)))) {
  "cf"
} else if (any(sapply(DI_EXTS,   function(e) endsWith(lower, e)))) {
  "di"
} else if (any(sapply(SCAN_EXTS, function(e) endsWith(lower, e)))) {
  "scan"
} else {
  cat(sprintf("Unrecognised IRMS extension: %s\n", basename(infile)), file = stderr())
  quit(status = 1)
}

# ── Read ──────────────────────────────────────────────────────────────────────

iso <- tryCatch({
  switch(file_type,
    cf   = iso_read_continuous_flow(infile),
    di   = iso_read_dual_inlet(infile),
    scan = iso_read_scan(infile)
  )
}, error = function(e) {
  cat(sprintf("isoreader error: %s\n", conditionMessage(e)), file = stderr())
  quit(status = 1)
})

n_files <- length(iso)
if (n_files == 0) {
  cat("isoreader returned 0 files — nothing to export.\n", file = stderr())
  quit(status = 1)
}

# ── file_info ─────────────────────────────────────────────────────────────────
# iso_remove_list_columns() drops nested list columns that can't serialise to CSV.

fi <- tryCatch(
  iso_get_file_info(iso) |> iso_remove_list_columns(),
  error = function(e) {
    message("file_info unavailable: ", conditionMessage(e))
    data.frame(file_id = basename(infile), error = conditionMessage(e))
  }
)
write_csv(safe_df(fi), file.path(outdir, "file_info.csv"))

# ── vendor_table ──────────────────────────────────────────────────────────────
# For CF: isoreader returns one row per peak per file.
# For DI: one row per cycle per block per file.
# We left-join file_info so downstream Python sees Analysis, Identifier 1, etc.
# alongside the measurement data — exactly the shape of a manual CSV export.

vt <- tryCatch({
  raw_vt <- iso_get_vendor_data_table(iso) |> iso_remove_list_columns() |> safe_df()

  # Join file-level metadata (Analysis, Identifier 1, Method…) to each peak row
  join_cols <- intersect(names(fi), names(raw_vt))
  if ("file_id" %in% names(fi) && "file_id" %in% names(raw_vt)) {
    fi_slim <- fi |> select(-any_of(setdiff(join_cols, "file_id")))
    raw_vt  <- left_join(raw_vt, fi_slim, by = "file_id", suffix = c("", ".fi"))
  }

  # For CF files: run isoprocessor peak-table mapping so column names are
  # standardised (Nr, Rt, Ampl, BGD, Area, d.13C.12C, d.15N.14N …).
  # iso_set_peak_table_from_isodat_vendor_data_table() only applies to Isodat
  # CF files (.dxf / .cf); guard with tryCatch so non-Isodat CF files still work.
  if (file_type == "cf") {
    iso <- tryCatch(
      iso |> iso_set_peak_table_from_isodat_vendor_data_table(),
      error = function(e) iso   # Elementar/Nu: leave peak table untouched
    )
    pt <- tryCatch(
      iso_get_peak_table(iso) |> iso_remove_list_columns() |> safe_df(),
      error = function(e) NULL
    )
    if (!is.null(pt) && nrow(pt) > 0) {
      # Attach file-level metadata to the standardised peak table too
      if ("file_id" %in% names(fi) && "file_id" %in% names(pt)) {
        fi_slim2 <- fi |> select(-any_of(setdiff(intersect(names(fi), names(pt)), "file_id")))
        pt <- left_join(pt, fi_slim2, by = "file_id", suffix = c("", ".fi"))
      }
      # Write both: raw vendor table AND isoprocessor peak table
      write_csv(raw_vt, file.path(outdir, "vendor_table_raw.csv"))
      raw_vt <- pt    # preferred output is the standardised peak table
    }
  }

  raw_vt
}, error = function(e) {
  message("vendor_table unavailable: ", conditionMessage(e))
  data.frame()
})
write_csv(vt, file.path(outdir, "vendor_table.csv"))

# ── raw_signals ───────────────────────────────────────────────────────────────
# Continuous mass traces (mV vs time) — large; write only if present.

rs <- tryCatch({
  rd <- iso_get_raw_data(iso) |> iso_remove_list_columns() |> safe_df()
  if ("file_id" %in% names(fi) && "file_id" %in% names(rd)) {
    fi_slim3 <- fi |> select(file_id, any_of(c("Analysis", "Identifier 1", "Identifier 2")))
    rd <- left_join(rd, fi_slim3, by = "file_id", suffix = c("", ".fi"))
  }
  rd
}, error = function(e) {
  data.frame()
})
write_csv(rs, file.path(outdir, "raw_signals.csv"))

# ── Done ──────────────────────────────────────────────────────────────────────
cat(sprintf(
  "OK: %d file(s) | type=%s | peaks=%d | signals=%d\n",
  n_files, file_type, nrow(vt), nrow(rs)
))
