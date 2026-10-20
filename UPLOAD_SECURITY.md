# Upload Security

This note describes the current file-confinement controls for uploaded
migrations handled by the REST API and web UI. It is not a general security
guarantee for arbitrary migration inputs or deployment environments.

## Upload handling

- The server creates private temporary upload and job/output directories. Each
  migration runs in a separate Python subprocess. The server passes the
  server-owned `TTPBI_UPLOAD_ROOT` and a private `TTPBI_EXTRACT_DIR` to that
  child process; it does not set the upload root in the shared server process.
  Extraction staging is inside the job's private output directory.
- Uploaded names are reduced to a validated basename. Before extraction,
  workbook/package references and archive members are checked: parent
  traversal, absolute paths (including POSIX-rooted, drive-qualified and UNC
  paths), and archive symlinks are rejected.
- While the server-owned upload root is set, local source lookup is confined to
  the uploaded tree; it does not search parent directories for data files.
  Paths are checked using canonical `realpath`/`commonpath` containment, and
  symlinks and Windows reparse points (including junctions) are rejected.
- The dedicated child installs a process-wide Python audit hook intended to
  check late file opens and copy/move/link operations, including calls from
  worker threads. The Windows native `CopyFile2` fast path used by modern
  Python's `shutil.copy2` is checked too, including pre-imported aliases.
  Reads are limited to the upload/output or approved application/runtime
  files, and writes to private output. A denied operation causes a failed
  worker exit even if a generation helper catches the exception.
  Generated download archives are walked and checked
  again; only regular files confined to the recorded output/job directories
  are included.
- The local CLI remains a trusted local workflow: when
  `TTPBI_UPLOAD_ROOT` is absent, its existing local source discovery behavior
  is unchanged.

REST callers continue to submit the `model_mode` field (`import`,
`directquery`, or `composite`). The server forwards its value to the CLI as
`--mode`; `model_mode` is not a CLI flag.

## Regression checks

Run from the repository with its existing virtual environment:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_upload_source_confinement.py tests\test_sprint110_api_server.py tests\test_api_v2.py tests\test_security.py tests\test_local_data_collection.py -q
```

Persistent tests cover relative CSV and TWBX migrations, rejected references,
symlinks/junctions when supported, late threaded reads/copies, distinct worker
roots, REST/UI/fallback entry points, archive confinement, and trusted local
CLI behavior. REST rejects unknown options explicitly rather than silently
discarding them.

Webhooks reject HTTP redirects rather than following an unchecked destination.
Delivery failures are logged; the migration result remains available through
the job status endpoint.

## Security boundary and deployment

These checks are application-level path and file-operation controls, **not an
operating-system sandbox**. They do not isolate the subprocess from the host
kernel, and they do not establish safety against every parser/runtime flaw,
resource-exhaustion attack, or operation performed by native code outside the
guarded Python file APIs. Untrusted plugins or native extensions are outside
this boundary and must be treated as trusted code.

Do not treat these controls alone as sufficient for hostile Internet-facing
uploads. For that threat model, run the service and workers under a
low-privilege OS account or an OS/container sandbox with narrowly scoped file
and network access. API authentication and binding configuration are separate
controls; an API key does not replace process isolation.
