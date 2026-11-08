"""Row processing system.

A controller that sends "write a row" jobs to model APIs, checks the rows, hands
them to review agents, collects verdicts and ships accepted rows to a dataset
repository. Every step is recorded as an event so a run can stop at any moment
and resume where it left off.
"""

__version__ = "0.2.8.dev0"
