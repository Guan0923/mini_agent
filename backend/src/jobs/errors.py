"""Stable exceptions for job registration, admission, and shutdown.

None of these exceptions carry owner identities, environments, command
lines, or unredacted job errors.
"""


class JobRegistrationError(RuntimeError):
    """A job could not be registered, started, or unregistered."""


class JobNotFound(JobRegistrationError):
    """No record exists for the requested job id."""


class JobScopeClosed(RuntimeError):
    """An operation was attempted on a closed scope."""


class JobAdmissionRejected(RuntimeError):
    """A job was rejected because no slot was available (``reject`` mode)."""


class JobAdmissionTimeout(RuntimeError):
    """A queued job was not admitted within its queue timeout."""


class JobQueueFull(RuntimeError):
    """A job could not be queued because a queue limit was reached."""


class JobCloseTimeout(RuntimeError):
    """A scope close exceeded its shared deadline."""
