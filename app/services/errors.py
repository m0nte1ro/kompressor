"""Application errors; their HTTP representation belongs to the transport layer."""


class NotFound(LookupError):
    pass


class Conflict(ValueError):
    pass


class InvalidOperation(ValueError):
    pass
