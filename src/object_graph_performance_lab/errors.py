"""User-facing failures. The CLI prints these and exits nonzero."""


class LabError(Exception):
    """A problem the operator can act on. The message must not contain secrets."""
