class ToolExecutionError(Exception):
    """Base tool execution error."""


class ToolTimeoutError(ToolExecutionError):
    """Transient timeout error."""


class ToolMalformedResponseError(ToolExecutionError):
    """Tool returned malformed payload."""


class ToolPartialDataError(ToolExecutionError):
    """Tool returned partial data."""


class TransientToolError(ToolExecutionError):
    """Transient failure that should be retried by Celery."""
