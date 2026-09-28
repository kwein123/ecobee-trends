"""Utility functions for the python-ecobee-api library."""
import errno
import os
from typing import Optional

try:
    import simplejson as json
except ImportError:
    import json

from .const import _LOGGER


def _write_atomically(filename: str, text: str) -> None:
    """Replace ``filename`` with ``text`` so it is never seen half-written.

    Local change in ecobee-trends (not in upstream python-ecobee-api): the
    token file is the only copy of a refresh token that ecobee rotates, so a
    crash or power cut mid-write must not leave it empty. Writes a private
    temp file in the same directory, flushes it to disk, then renames it over
    the original.
    """
    directory = os.path.dirname(os.path.abspath(filename))
    tmp = os.path.join(directory, f".{os.path.basename(filename)}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fdesc:
            fdesc.write(text)
            fdesc.flush()
            os.fsync(fdesc.fileno())
        try:
            os.replace(tmp, filename)
        except OSError as err:
            if err.errno not in (errno.EBUSY, errno.EXDEV):
                raise
            # The file itself is a bind mount (can't be renamed over), so the
            # best available is the plain in-place write.
            with open(filename, "w") as fdesc:
                fdesc.write(text)
                fdesc.flush()
                os.fsync(fdesc.fileno())
            os.unlink(tmp)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def config_from_file(filename: str, config: dict = None) -> Optional[str]:
    """Reads/writes json from/to a filename."""
    if config:
        # We're writing configuration
        try:
            _write_atomically(filename, json.dumps(config))
            return True
        except IOError as error:
            _LOGGER.exception(error)
            return False
    else:
        # We're reading config
        if os.path.isfile(filename):
            try:
                with open(filename, "r") as fdesc:
                    return json.loads(fdesc.read())
            except IOError as error:
                _LOGGER.exception(error)
                return False
        else:
            return {}

def convert_to_bool(input) -> bool:
    return str(input).lower() in ["true", "1", "t", "y", "yes"]