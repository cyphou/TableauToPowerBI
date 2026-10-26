# Upload Security

This note describes the current file-confinement controls for uploaded
migrations handled by the REST ZIP API and optional Streamlit/fallback upload
UI. It is not a general security
guarantee for arbitrary migration inputs or deployment environments.

The recommended [local browser runner](docs/LOCAL_WEB_UI.md) is different:
it accepts trusted local folder paths and does not upload workbooks. Its
loopback/origin/session controls do not turn local migrations into the
upload-confined or OS-sandboxed workflow described here.

## Upload handling

- The server creates private temporary upload and job/output directories. Each
  migration runs in a separate Python subprocess. The server passes the
  server-owned `TTPBI_UPLOAD_ROOT` and a private `TTPBI_EXTRACT_DIR` to that
  child process; it does not set the upload root in the shared server process.
  Extraction staging is inside the job's private output directory.
- Native Hyper-to-Parquet writing needs an entirely ASCII staging path because
  of the Windows SDK's Unicode-output-path defect. Its private transit must
  remain inside the authorized job boundary. If no writable authorized ASCII
  path exists, the export fails; it does not use an external temporary directory
  or relax confinement. Python publishes the finished file at the requested
  destination. Complete PBIP migrations also reject capped/sample-only Hyper
  exports and unresolved Import bindings.
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

## Hyper project metadata

- The read-only Hyper import check reads absolute paths that are stored in the
  generated project: the `Migration_HyperIdentity` TMDL annotation, the
  `Data/hyper_exports.json` manifest, the `DataFolder` parameter and
  `File.Contents` literals. Those values are untrusted input, so none of them
  is ever resolved through the filesystem. Each one is normalized as text
  (`lexical_path_key`: absolute form, separators, case, `.` and `..`) and
  compared with the keys of the `Data` folder the operator selected: its own
  spelling and its resolved location. A UNC or device-namespace path
  (`\\host\share`, `\\?\UNC\...`, `\\.\...`, `\??\...`), or a local-looking
  path that runs through a symlink or junction, therefore cannot make
  verification open a connection to a host it names. A UNC output folder that
  the operator selected still matches itself, and so does a project that the
  operator reached through a link of their own choosing.
- Inside the project, a `Data` directory, `expressions.tmdl`, export file or
  manifest that is a symlink, a Windows reparse point (junction) or a
  multiply-linked file is reported and is never opened, listed or followed.
  That includes the manifest read and the Hyper artifact scan of the sampling
  check, which does not descend into links either.
- The cost of this rule is that an alias of the `Data` folder through a link
  or an 8.3 short name, written into the project metadata, is not accepted.
  Regenerate the project at its real location instead.
- The scope is the Hyper checks. The generic project scanners (the TMDL, PBIR
  and report walkers of the quality gate, the openability check) and the MCP
  `quality_report(project_dir)` tool read the project tree as it is given and
  do not test for links. Treat any project that this tool did not generate as
  untrusted input, and do not point those readers at it from a workstation
  that can reach hostile network shares.
- A drive letter that the user mapped to a network share (for example `Z:` to
  `\\host\share`) cannot be told apart from a local drive by its text and is
  treated as local. Project metadata cannot create that mapping.
- The generated TMDL annotation, `Data/hyper_exports.json` and the quality
  report contain the absolute path of the source workbook. The exact path is
  part of the extract identity, and the staged CSV/Parquet files and packaged
  Excel sources are bound by absolute path by design. Keep generated projects
  and reports inside the trust zone of the workstation that produced them and
  review them before sharing them outside it.

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
