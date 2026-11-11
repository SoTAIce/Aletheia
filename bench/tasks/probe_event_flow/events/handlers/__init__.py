"""Importing this package registers the handlers."""
from events.handlers import stock  # noqa: F401
from events.handlers import fraud  # noqa: F401
from events.handlers import invoices  # noqa: F401
from events.handlers import tax  # noqa: F401
from events.handlers import payments  # noqa: F401
from events.handlers import ledger  # noqa: F401
from events.handlers import loyalty  # noqa: F401
from events.handlers import shipping  # noqa: F401
from events.handlers import mailer  # noqa: F401
from events.handlers import audit  # noqa: F401
from events.handlers import analytics  # noqa: F401
from events.handlers import reports  # noqa: F401
