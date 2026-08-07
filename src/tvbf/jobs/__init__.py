"""Scheduled jobs invoked as processes rather than through the HTTP API.

Each module here is a `python -m tvbf.jobs.<name>` entrypoint run on a Coolify
schedule. The process *is* the run, so its exit code is the result — no 202, no
polling, and no admin token leaving the host.
"""
