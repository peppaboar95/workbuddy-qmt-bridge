class BridgeError(Exception):
    """An error with a stable API-facing code."""

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class ValidationError(BridgeError):
    def __init__(self, message, details=None):
        super().__init__("INVALID_REQUEST", message, details)

