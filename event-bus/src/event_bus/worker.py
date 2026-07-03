"""RQ worker entry point.  Run with: python -m event_bus.worker"""

import redis
from rq import Worker

from event_bus.config import settings
from event_bus.limits import reconcile_slots


def main() -> None:
    conn = redis.from_url(settings.redis_url, decode_responses=False)
    # A restart kills in-flight jobs before their release_slot() finally runs, leaking the
    # per-role concurrency counters (a leaked reviewer slot blocks all code reviews). A
    # fresh worker has zero in-flight jobs, so reset the counters to the truthful value.
    reconcile_slots(conn)
    worker = Worker(queues=["agent-jobs"], connection=conn)
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    main()
