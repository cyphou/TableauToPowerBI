"""Migration progress tracker with callback support.

Provides ``MigrationProgress`` — a lightweight observer that tracks
step-level progress and can trigger user-supplied callbacks on each
step transition.  Works in both CLI (prints a progress bar) and
programmatic (callback) modes.

Usage::

    from powerbi_import.progress import MigrationProgress

    progress = MigrationProgress(total_steps=5, on_step=my_callback)
    progress.start("Extracting datasources")
    # ... do work ...
    progress.complete("Extracted 12 tables")
    progress.start("Converting DAX formulas")
    # ...
"""

import os
import sys
import time


class MigrationProgress:
    """Tracks migration progress across steps.

    Args:
        total_steps: Total number of migration steps.
        on_step: Optional callback ``(step_index, step_name, status, message)``
                 called on each ``start()`` and ``complete()`` call.
        show_bar: If True (default), prints a progress bar to stderr.
        bar_width: Width of the progress bar in characters.
    """

    def __init__(self, total_steps=5, on_step=None, show_bar=True, bar_width=40):
        self.total_steps = total_steps
        self.on_step = on_step
        self.show_bar = show_bar
        self.bar_width = bar_width
        self._current = 0
        self._steps = []
        self._start_time = None
        self._step_start = None

    def start(self, step_name):
        """Mark a new step as in progress.

        Args:
            step_name: Human-readable description of the step.
        """
        self._current += 1
        self._step_start = time.time()
        if self._start_time is None:
            self._start_time = self._step_start

        self._steps.append({
            'index': self._current,
            'name': step_name,
            'status': 'in_progress',
            'started_at': self._step_start,
        })

        if self.on_step:
            self.on_step(self._current, step_name, 'in_progress', '')

        if self.show_bar:
            self._print_bar(step_name, 'in_progress')

    def complete(self, message=''):
        """Mark the current step as complete.

        Args:
            message: Optional completion message.
        """
        if self._steps:
            step = self._steps[-1]
            step['status'] = 'complete'
            step['message'] = message
            elapsed = time.time() - (self._step_start or time.time())
            step['elapsed'] = round(elapsed, 2)

            if self.on_step:
                self.on_step(step['index'], step['name'], 'complete', message)

            if self.show_bar:
                self._print_bar(step['name'], 'complete', message)

    def fail(self, error=''):
        """Mark the current step as failed.

        Args:
            error: Error description.
        """
        if self._steps:
            step = self._steps[-1]
            step['status'] = 'failed'
            step['error'] = error
            elapsed = time.time() - (self._step_start or time.time())
            step['elapsed'] = round(elapsed, 2)

            if self.on_step:
                self.on_step(step['index'], step['name'], 'failed', error)

            if self.show_bar:
                self._print_bar(step['name'], 'failed', error)

    def skip(self, step_name, reason=''):
        """Record a skipped step.

        Args:
            step_name: Name of the skipped step.
            reason: Why it was skipped.
        """
        self._current += 1
        self._steps.append({
            'index': self._current,
            'name': step_name,
            'status': 'skipped',
            'message': reason,
        })
        if self.on_step:
            self.on_step(self._current, step_name, 'skipped', reason)
        if self.show_bar:
            self._print_bar(step_name, 'skipped', reason)

    def summary(self):
        """Return a summary dict of all steps.

        Returns:
            dict: ``{'steps': [...], 'total_elapsed': float,
                     'completed': int, 'failed': int, 'skipped': int}``
        """
        total_elapsed = time.time() - (self._start_time or time.time())
        return {
            'steps': list(self._steps),
            'total_elapsed': round(total_elapsed, 2),
            'completed': sum(1 for s in self._steps if s['status'] == 'complete'),
            'failed': sum(1 for s in self._steps if s['status'] == 'failed'),
            'skipped': sum(1 for s in self._steps if s['status'] == 'skipped'),
        }

    # ── Internal ──

    def _print_bar(self, step_name, status, message=''):
        """Print a progress bar to stderr."""
        pct = self._current / max(self.total_steps, 1)
        filled = int(self.bar_width * pct)
        bar = '█' * filled + '░' * (self.bar_width - filled)

        icons = {
            'in_progress': '⏳',
            'complete': '✅',
            'failed': '❌',
            'skipped': '⏭️',
        }
        icon = icons.get(status, '  ')

        line = f"\r  {icon} [{bar}] {self._current}/{self.total_steps} {step_name}"
        if message:
            line += f" — {message}"

        sys.stderr.write(line)
        if status in ('complete', 'failed', 'skipped'):
            sys.stderr.write('\n')
        sys.stderr.flush()


class NullProgress:
    """No-op progress tracker (silent)."""

    def start(self, step_name):
        pass

    def complete(self, message=''):
        pass

    def fail(self, error=''):
        pass

    def skip(self, step_name, reason=''):
        pass

    def summary(self):
        return {'steps': [], 'total_elapsed': 0, 'completed': 0, 'failed': 0, 'skipped': 0}


class StreamingProgress:
    """Throttled progress for large row/file operations.

    Progress records are written to stderr. Interactive terminals get a
    carriage-return ASCII bar; non-interactive streams (including the local
    browser log tunnel) get newline-delimited, flushed records.
    """

    def __init__(self, stream=None, quiet=False, interval=2.0, clock=None):
        self.stream = stream or sys.stderr
        self.quiet = bool(quiet)
        self.interval = max(float(interval), 2.0)
        self._clock = clock or time.monotonic
        try:
            self._tty = bool(self.stream.isatty())
        except (AttributeError, OSError, ValueError):
            self._tty = False
        self.started_at = None
        self.last_output_at = None
        self.label = ''
        self.rows_written = 0
        self.total_rows = None

    def start(self, label, total_rows=None):
        """Emit the start record before any potentially expensive COUNT."""
        self.label = str(label)
        self.started_at = self._clock()
        self.last_output_at = None
        self.rows_written = 0
        self.total_rows = total_rows
        self._write(f"Hyper export started: {self.label}", force=True)

    def set_total(self, total_rows):
        """Set the known source-row count, or ``None`` when it is unknown."""
        self.total_rows = total_rows

    def update(self, rows_written, total_rows=None):
        """Record rows only after their writer has accepted them."""
        self.rows_written = max(0, int(rows_written))
        if total_rows is not None:
            self.total_rows = total_rows
        now = self._clock()
        if (self.last_output_at is not None
                and now - self.last_output_at < self.interval):
            return
        start = self.started_at if self.started_at is not None else now
        elapsed = max(now - start, 0.0)
        rate = self.rows_written / elapsed if elapsed > 0 else 0.0
        if isinstance(self.total_rows, int) and self.total_rows >= 0:
            if self.total_rows == 0:
                detail = f"{self.rows_written}/0 rows"
            else:
                percent = min(99, int(self.rows_written * 100 / self.total_rows))
                remaining = max(self.total_rows - self.rows_written, 0)
                eta = remaining / rate if rate > 0 else None
                detail = f"{self.rows_written}/{self.total_rows} rows ({percent}%)"
                if eta is not None:
                    detail += f", {rate:.1f} rows/s, ETA {eta:.0f}s"
                else:
                    detail += f", {rate:.1f} rows/s"
        else:
            detail = f"{self.rows_written} rows, {rate:.1f} rows/s"
        self._write(
            f"Hyper export progress: {self.label}: {detail}",
            progress=True, detail=detail)

    def finish(self, rows_written, total_rows=None, output_path=None,
               sampled=False):
        """Emit completion only after the caller flushes and publishes output."""
        if rows_written is not None:
            self.rows_written = max(0, int(rows_written))
        if total_rows is not None:
            self.total_rows = total_rows
        now = self._clock()
        start = self.started_at if self.started_at is not None else now
        elapsed = max(now - start, 0.0)
        try:
            byte_size = os.path.getsize(output_path) if output_path else None
        except OSError:
            byte_size = None
        details = (
            [f"{self.rows_written} rows"] if rows_written is not None
            else ["row count unknown"])
        if isinstance(self.total_rows, int) and self.total_rows >= 0:
            details.append(f"{self.total_rows} source rows")
        if byte_size is not None:
            details.append(f"{byte_size} bytes")
        else:
            details.append("file size unavailable")
        details.append(f"{elapsed:.1f}s")
        if sampled:
            details.append("sampled/capped")
        self._write(
            f"Hyper export complete: {self.label}: {', '.join(details)}",
            force=True,
            final=True,
        )

    def fail(self, error, rows_written=None):
        """Report a failed export without implying that partial rows survived."""
        if rows_written is not None:
            self.rows_written = max(0, int(rows_written))
        now = self._clock()
        start = self.started_at if self.started_at is not None else now
        elapsed = max(now - start, 0.0)
        self._write(
            f"Hyper export failed: {self.label} after "
            f"{self.rows_written} written rows ({elapsed:.1f}s): {error}",
            force=True,
            final=True,
        )

    def _write(self, message, force=False, final=False, progress=False,
               detail=''):
        if self.quiet:
            return
        now = self._clock()
        if not force and self.last_output_at is not None:
            if now - self.last_output_at < self.interval:
                return
        if self._tty:
            if progress and isinstance(self.total_rows, int) and self.total_rows > 0:
                percent = min(99, int(
                    self.rows_written * 100 / self.total_rows))
                width = 24
                filled = int(width * percent / 100)
                bar = "#" * filled + "-" * (width - filled)
                line = (
                    f"\rHyper export [{bar}] {percent:3d}% "
                    f"{self.label}: {detail}")
            elif progress:
                line = f"\rHyper export {self.label}: {detail}"
            else:
                line = "\r" + message
            if final:
                line += "\n"
        else:
            line = message + "\n"
        try:
            self.stream.write(line)
            self.stream.flush()
            self.last_output_at = now
        except (OSError, ValueError):
            # Progress must never abort data export.
            self.quiet = True
