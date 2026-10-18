class AppError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        self.code, self.message, self.status = code, message, status
        super().__init__(code)


def require(condition: bool, code: str, message: str, status: int = 409) -> None:
    if not condition:
        raise AppError(code, message, status)
