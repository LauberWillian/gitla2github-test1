# violet: An asyncio background job library
# Copyright 2019-2020, elixi.re Team and the violet contributors
# SPDX-License-Identifier: LGPL-3.0

import datetime
import uuid
import asyncio
import logging
import random
from typing import List, Any, Dict, Optional, Union, Set, Tuple
from collections import defaultdict

from hail import Flake, FlakeFactory

from violet.errors import TaskExistsError, QueueExistsError
from violet.models import Queue, JobDetails
from violet.queue_worker import queue_worker, queue_poller
from violet.utils import execute_with_json
from violet.event import JobEvent
from violet.fail_modes import FailMode, LogOnly, RaiseErr

log = logging.getLogger(__name__)


class EmptyAsyncContext:
    def __init__(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, type, value, traceback):
        return None


class JobManager:
    """Manage background jobs."""

    def __init__(
        self,
        *,
        db=None,
        context_function=None,
        node_id: Optional[int] = None,
    ):
        self.loop = asyncio.get_event_loop()
        self.db = db
        self.tasks: Dict[str, asyncio.Task] = {}
        self.queues: Dict[str, Queue] = {}
        self.factory = FlakeFactory(node_id or random.randint(0, 65535))

        self.events: Dict[str, JobEvent] = defaultdict(JobEvent)
        self.start_events: Dict[str, JobEvent] = defaultdict(JobEvent)

        self.empty_waiters: Dict[str, asyncio.Task] = {}
        self.empty_start_waiters: Dict[str, asyncio.Task] = {}

        self.context_creator = context_function or EmptyAsyncContext
        self._poller_sets: Dict[str, Set[str]] = defaultdict(set)

    def exists(self, task_id: str) -> bool:
        """Return if a given task exists in the current running task list."""
        return task_id in self.tasks

    def _create_task(self, task_id: str, *, main_coroutine):
        """Wrapper around loop.create_task that ensures unique task ids
        internally."""
        if task_id in self.tasks:
            raise TaskExistsError(f"Task '{task_id}' already exists")

        task = self.loop.create_task(main_coroutine)
        self.tasks[task_id] = task
        return task

    async def _wrapper(self, function, args, task_id, **kwargs):
        """Wrapper for coroutines, wrapping them in try/excepts for logging"""
        try:
            log.debug("task tick: %r", task_id)
            async with self.context_creator():
                return await function(*args)
        except asyncio.CancelledError:
            log.debug("task %r cancelled", task_id)
        except Exception as exc:
            fail_mode: FailMode = kwargs.get("fail_mode") or LogOnly()

            # state here serves as a way for failure modes to be fully
            # contained. the instantiation of a failure mode only declares
            # its configuration, its behavior becomes completely separate
            # inside the auto-genned state dict.
            state = kwargs.get("_wrapper_state") or {}
            retry = await fail_mode.handle(JobDetails(task_id), exc, state)

            if retry:
                # we need to share state for this job in some way and this is
                # the best way i found while developing.
                kwargs["_wrapper_state"] = state
                return await self._wrapper(function, args, task_id, **kwargs)
        finally:
            self._remove_task(task_id)

    def spawn(self, function, args: List[Any], *, name: str, **kwargs) -> asyncio.Task:
        """Spawn the given function in the background.

        This is a wrapper around loop.create_task that gives you proper logging
        and optional recovery capabilities.

        If you wish the background task is fully recoverable even in the face
        of a crash, use a job queue.
        """

        mode = kwargs.get("fail_mode")
        if mode is not None and not isinstance(mode, FailMode):
            raise ValueError("Failure modes must be instances of FailMode.")

        task = self._create_task(
            name, main_coroutine=self._wrapper(function, args, name, **kwargs)
        )

        if kwargs.get("set_asyncio_name", True):
            task.set_name(name)

        return task

    def spawn_periodic(
        self,
        function,
        args: List[Any],
        *,
        period: float = 5,
        name: str,
        reverse: bool = False,
        **kwargs,
    ):
        """Spawn a function that ticks itself periodically every
        ``period`` seconds.

        ``reverse`` decides which goes first, the sleep call, or the function
        call. Specific usage of this can apply.
        """

        async def ticker_func():
            while True:
                if reverse:
                    await asyncio.sleep(period)

                await function(*args)

                if not reverse:
                    await asyncio.sleep(period)

        task = self._create_task(
            name, main_coroutine=self._wrapper(ticker_func, [], name, **kwargs)
        )

        if kwargs.get("set_asyncio_name", True):
            task.set_name(name)

        return task

    def register_job_queue(self, cls) -> None:
        """Create a job queue.

        The job queue MUST be declared at the start of the application so
        job recovery can happen as soon as possible. It is also required to
        declare the queue before any queue operations.

        To enhance the concurrency of the queue on high error rates, the first
        consideration is fixing the error first, and the second, is to raise
        the ``workers`` count. It is not recommended to raise it to very
        high levels, as there will be a lot of clashing as workers try to lock
        the same job multiple times.

        Users of violet can wait for specific events of a job (currently, start
        and end). While the end event is static and is always called, the
        start event can be customized and set in a different point in time
        inside the job itself (instead of it being at lock time). This might
        be interesting for users that want to have a jobs' state setup after
        they hear about the job start. Set ``custom_start_event`` to enable
        this functionality.

        While creating a job queue, a single worker, called the "poller", is
        spawned. The poller checks every second for any outstanding jobs
        to run at its current point in time. An outstanding job is a job that
        was scheduled in the future, but should be run right now.

        ``poller_takes`` sets the maximum amount of jobs that will be
        taken by the poller and be inserted into the job queue.
        """
        # TODO: move those docs to JobQueue docstrings
        try:
            queue_name = getattr(cls, "name")
        except AttributeError:
            raise TypeError("Queues must have the name attribute.")

        cls.fail_mode = cls.fail_mode or RaiseErr(log_error=True)

        if queue_name in self.queues:
            raise QueueExistsError()

        queue = Queue(queue_name, cls, asyncio.Queue())
        cls._sched = self
        self.queues[queue_name] = queue

        self.spawn(queue_poller, [queue], name=f"queue_poller_{queue_name}")
        for worker_id in range(cls.workers):
            self.spawn(
                self._queue_worker_wrapper,
                [queue, worker_id],
                name=f"queue_worker_{queue.name}_{worker_id}",
            )

    async def _queue_worker_wrapper(self, queue: Queue, worker_id: int):
        try:
            async with self.context_creator():
                await queue_worker(queue, worker_id)

        except asyncio.CancelledError:
            log.debug("queue worker for %r cancelled", queue.name)
        except Exception:
            log.exception("Queue worker for queue %r failed", queue.name)

    async def raw_push(
        self,
        cls,
        args: Tuple[Any],
        *,
        name: Optional[str] = None,
        scheduled_at: Optional[datetime.datetime] = None,
    ) -> Flake:
        """Push data to a job queue."""
        if not isinstance(args, tuple):
            raise TypeError("arguments must be a tuple")

        if len(args) != len(cls.args):
            raise TypeError("Invalid argument arity")

        log.debug("try push %r %r", cls.name, args)
        actual_scheduled_at = scheduled_at or datetime.datetime.utcnow()

        job_id = self.factory.get_flake()
        name = name or uuid.uuid4().hex

        arg_line_columns: str = ""
        if cls.args:
            # for any declared args on the job queue,
            # generate "column_a, column_b"
            arg_line_columns = f', {",".join(cls.args)}'

        arg_line_values: str = ""
        if args:
            # for any incoming args, generate "$4, $5..."
            arg_line_values = ", " + ",".join(
                f"${index + 4}" for index in range(len(args))
            )

        await execute_with_json(
            self.db,
            f"""
            INSERT INTO {cls.name}
                (job_id, name, scheduled_at{arg_line_columns})
            VALUES
                ($1, $2, $3{arg_line_values})
            """,
            str(job_id),
            name,
            actual_scheduled_at,
            *args,
        )

        queue = self.queues[cls.name]

        # Only dispatch to asyncio_queue if the task is meant to be run now,
        # which happens when either is true:
        #  - No scheduled_at
        #  - Time difference between scheduled_at and now is lower than
        #  	the queue's tick period (defined by the poller_takes
        #  	queue configuration value).
        if scheduled_at is None:
            should_schedule_now = True
        else:
            now = datetime.datetime.utcnow()
            time_until_hit = scheduled_at - now
            should_schedule_now = time_until_hit.total_seconds() < cls.poller_seconds

        if should_schedule_now:
            queue.asyncio_queue.put_nowait(job_id)

        return job_id

    def _remove_task(self, task_id: str) -> None:
        """Remove a job from the internal task list."""
        try:
            self.tasks.pop(task_id)
        except KeyError:
            pass

    def stop(self, task_id: str) -> None:
        log.debug("stopping task %r", task_id)

        try:
            task = self.tasks[task_id]
            task.cancel()
        except KeyError:
            log.warning("unknown task to cancel: %r", task_id)
        finally:
            # as a last measure, try to pop() the job
            # post-cancel. if that fails, the job probably
            # already cleaned itself.
            self._remove_task(task_id)

    async def stop_all(self, wait: bool = False, timeout: Optional[int] = None) -> None:
        """Stop the job manager by cancelling all tasks."""
        log.debug("cancelling %d tasks", len(self.tasks))

        tasks = []

        for task_id in list(self.tasks.keys()):
            tasks.append(self.tasks[task_id])
            self.stop(task_id)

        if wait and tasks:
            await asyncio.wait(tasks, timeout=timeout)

    async def wait_job_start(
        self, any_job_id: Union[str, Flake], *, timeout=None
    ) -> None:
        """Wait for a job to start.

        Start can be defined by:
         - When the queue worker acquires the jobs
         - When the queue handler signals it's start

        That is a queue setting.

        It is recommended for queues to signal themselves as custom,
        if they have any state to setup, or else, users of this function
        might get completely empty state.
        """

        job_id = str(any_job_id)

        async def waiter():
            await self.start_events[job_id].empty_event.wait()
            self.start_events.pop(job_id)
            self.empty_start_waiters.pop(job_id)

        if job_id not in self.empty_start_waiters:
            self.empty_start_waiters[job_id] = self.spawn(
                waiter, [], name=f"empty_start_waiter:{job_id}"
            )

        await asyncio.wait_for(self.start_events[job_id].wait(), timeout)
