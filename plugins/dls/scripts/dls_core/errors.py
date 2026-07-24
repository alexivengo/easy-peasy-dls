"""DLS error taxonomy."""


class DLSError(Exception):
    """Base error rendered without a traceback by the CLI."""


class UsageError(DLSError):
    """Invalid user input or command usage."""


class IntegrityError(DLSError):
    """State, digest, or revision integrity failure."""


class LockError(DLSError):
    """Concurrent or stale state lock failure."""


class ConfigError(DLSError):
    """Invalid or unsafe repository configuration."""
