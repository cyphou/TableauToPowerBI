# Uploaded workbook source confinement

The Streamlit UI, fallback HTTP UI, and REST batch endpoint run migrations in
separate processes with the server-owned `TTPBI_UPLOAD_ROOT` environment variable.
Do not populate this variable from request options, workbook metadata, or batch
configuration. Workers inherit it; the threaded HTTP server does not modify its
own environment.

Uploaded file sources must use relative references inside their package or
private upload directory. Absolute paths, drive/UNC paths, parent traversal,
and paths escaping the allowed directory after `realpath` (including symbolic
links and Windows junctions) are rejected. Missing declared local files are
explicit failures, not permission to search the host. Parent-directory discovery
and author-machine path relocation are disabled in this context. The restriction
also covers generated DataFolder references, post-generation source collection,
spatial/CSV/image reads, and final downloadable archive members. A process-level
copy audit guard additionally confines generator copies to the upload and output
directories.

The REST ZIP endpoint extracts each workbook into a private directory. Sources
must be embedded in its TWBX/TDSX package; other loose ZIP entries are not source
files for that workbook. The Web UI accepts workbook files, not arbitrary host
source paths. Assessments use private temporary extraction directories.

The ordinary local CLI and local-path MCP tools remain trusted workflows:
without `TTPBI_UPLOAD_ROOT`, external declared sources, sibling/parent discovery,
and relocation retain their existing behavior. The environment variable is a
server-to-pipeline context marker, not a general OS sandbox. Hosts must still
isolate untrusted migrations from concurrent filesystem writers and use private
upload/output directories.
