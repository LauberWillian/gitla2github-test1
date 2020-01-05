# violet: An asyncio background job library
# Copyright 2019-2020, elixi.re Team and the violet contributors
# SPDX-License-Identifier: LGPL-3.0

import enum
import asyncio
import datetime
from typing import Iterable, Callable, Any, Awaitable, Optional
from dataclasses import dataclass


@dataclass
class Queue:
    name: str
    args: Iterable[type]
    function: Callable[..., Awaitable[Any]]
    takes: int
    period: int
    start_existing_jobs: bool

    task: Optional[asyncio.Task] = None


class JobState(enum.IntEnum):
    NotTaken = 0
    Taken = 1
    Completed = 2
    Error = 3


@dataclass
class QueueJobStatus:
    queue_name: str
    state: JobState
    fail_mode: str
    errors: str
    args: Iterable[type]
    inserted_at: datetime.datetime


@dataclass
class QueueJobContext:
    # TODO fix typing and recursive import that would happen for this maybe
    manager: Any
    job_id: str
