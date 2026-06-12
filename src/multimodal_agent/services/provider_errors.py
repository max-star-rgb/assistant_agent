"""Provider adapter error helpers."""


class ProviderAdapterError(ValueError):
    """Error raised by optional real provider adapters."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")
