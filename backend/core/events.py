import asyncio
import logging
from typing import Dict, List, Any

logger = logging.getLogger("apexalgo.events")

class EventBus:
    def __init__(self, max_queue_size: int = 1000):
        self.subscribers: Dict[str, List[asyncio.Queue]] = {}
        self._max_queue_size = max_queue_size

    def subscribe(self, event_type: str) -> asyncio.Queue:
        """Returns an asyncio.Queue that listens to a specific event type."""
        if event_type not in self.subscribers:
            self.subscribers[event_type] = []
        queue = asyncio.Queue(maxsize=self._max_queue_size)
        self.subscribers[event_type].append(queue)
        return queue

    async def publish(self, event_type: str, payload: Any):
        """Pushes data to all listening queues for the given event type."""
        if event_type in self.subscribers:
            for queue in self.subscribers[event_type]:
                try:
                    queue.put_nowait(payload)
                except asyncio.QueueFull:
                    logger.warning("EventBus queue full for %s, dropping oldest event", event_type)
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    queue.put_nowait(payload)

# Singleton instance exported for use across the application
event_bus = EventBus()
