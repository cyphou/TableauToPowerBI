"""Atomic publication of locally generated text artifacts."""

import os
import tempfile


def atomic_write_text(path, content, encoding='utf-8'):
    """Publish text through a sibling temporary file, retaining old bytes on error."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix='.' + os.path.basename(path) + '.', suffix='.tmp',
        dir=directory)
    stream = None
    try:
        try:
            stream = os.fdopen(fd, 'w', encoding=encoding)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        with stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
