"""RQ worker entry point.  Run with: python -m event_bus.worker"""

import redis
from rq import Worker

from event_bus.config import settings


def main() -> None:
    conn = redis.from_url(settings.redis_url, decode_responses=False)
    worker = Worker(queues=["agent-jobs"], connection=conn)
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    main()
