"""Agent dispatch — spawn headless CLI agents and track jobs."""

from mindsync.dispatch.runner import (
    AgentNotInstalledError,
    assert_arg_mode_spawn_safe,
    cancel_job,
    job_result,
    run_task,
    supervise_job,
)

__all__ = [
    "AgentNotInstalledError",
    "assert_arg_mode_spawn_safe",
    "cancel_job",
    "job_result",
    "run_task",
    "supervise_job",
]
