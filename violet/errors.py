# violet: An asyncio background job library
# Copyright 2019-2020, elixi.re Team and the violet contributors
# SPDX-License-Identifier: LGPL-3.0


class TaskExistsError(Exception):
    pass


class QueueExistsError(Exception):
    pass
