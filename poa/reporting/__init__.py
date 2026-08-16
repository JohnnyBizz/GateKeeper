"""Session reports — a written account of what the assistant decided.

The panel shows what is true right now. This writes down what was true all
session, in a file that outlives the process and can be read by someone who
was not sitting in front of it.
"""

from .session import SessionReport, build_report, collect, write_report

__all__ = ["SessionReport", "build_report", "collect", "write_report"]
