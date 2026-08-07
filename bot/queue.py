"""Priority download queue: VIP/admin jobs first, cancellable while queued."""

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Job:
    user_id: int
    chat_id: int
    message: Any          # the user's Message (for replies)
    status: Any           # the bot's status Message (for edits)
    platform: Any
    url: str
    cost: int
    is_admin: bool
    is_vip: bool
    size_limit: int | None  # bytes; None = unlimited (admin)
    enqueued_at: float = field(default_factory=time.time)
    state: str = "queued"   # queued | active | done | cancelled


class DownloadQueue:
    """Two-lane FIFO (priority lane for VIP/admin) consumed by worker tasks.

    All methods run on the single asyncio event loop, so plain deques are safe.
    One job per user at a time (queued or active).
    """

    def __init__(self) -> None:
        self._vip: deque[Job] = deque()
        self._norm: deque[Job] = deque()
        self._cond = asyncio.Condition()
        self._jobs: dict[int, Job] = {}  # user_id -> queued/active job

    async def put(self, job: Job) -> int:
        """Enqueue and return the job's 1-based position."""
        async with self._cond:
            self._jobs[job.user_id] = job
            lane = self._vip if (job.is_vip or job.is_admin) else self._norm
            lane.append(job)
            self._cond.notify()
            return self.position(job.user_id)

    async def get(self) -> Job:
        """Worker call: wait for the next runnable job (priority lane first)."""
        async with self._cond:
            while True:
                job = self._pop()
                if job is not None:
                    job.state = "active"
                    return job
                await self._cond.wait()

    def _pop(self) -> Job | None:
        for lane in (self._vip, self._norm):
            while lane:
                job = lane.popleft()
                if job.state == "queued":  # skip cancelled leftovers
                    return job
        return None

    def done(self, job: Job) -> None:
        job.state = "done"
        self._jobs.pop(job.user_id, None)

    def cancel(self, user_id: int) -> bool:
        """Cancel the user's job if still queued. Active jobs can't be cancelled."""
        job = self._jobs.get(user_id)
        if job is not None and job.state == "queued":
            job.state = "cancelled"
            self._jobs.pop(user_id, None)
            return True
        return False

    def get_job(self, user_id: int) -> Job | None:
        return self._jobs.get(user_id)

    def position(self, user_id: int) -> int:
        """1-based position among queued jobs; 0 if not queued."""
        job = self._jobs.get(user_id)
        if job is None or job.state != "queued":
            return 0
        pos = 1
        for lane in (self._vip, self._norm):
            for j in lane:
                if j.state != "queued":
                    continue
                if j is job:
                    return pos
                pos += 1
        return 0

    def snapshot(self) -> dict:
        vip_waiting = sum(1 for j in self._vip if j.state == "queued")
        normal_waiting = sum(1 for j in self._norm if j.state == "queued")
        active = [j for j in self._jobs.values() if j.state == "active"]
        return {
            "vip_waiting": vip_waiting,
            "normal_waiting": normal_waiting,
            "active": len(active),
            "active_jobs": [
                (j.user_id, j.platform.label, j.is_vip or j.is_admin) for j in active
            ],
        }
