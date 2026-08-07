import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageGrab
from screeninfo import get_monitors

from aethernet.typing import LoggerLike


class ScreenManager:
    def __init__(self, logger: LoggerLike = logging.getLogger(__name__)):
        monitors = get_monitors()
        self.monitor = next((m for m in monitors if m.is_primary), monitors[0] if monitors else None)
        self.width =  self.monitor.width
        self.height =  self.monitor.height
        x1 = self.monitor.x
        y1 = self.monitor.y
        x2 = x1 + self.width
        y2 = y1 + self.height
        self.monitor_info = (x1, y1, x2, y2)

        self._logger = logger
        self._executor = ThreadPoolExecutor(max_workers=1)

    def _grab_sync(self) -> Image.Image:
        return ImageGrab.grab(bbox=self.monitor_info)

    async def screenshot(self) -> Image.Image:
        self._logger.info("[screen] submitting to executor")
        loop = asyncio.get_running_loop()
        # noinspection PyTypeChecker
        img = await loop.run_in_executor(self._executor, self._grab_sync)
        self._logger.info("[screen] got image from executor")
        return img

    async def close(self) -> None:
        self._executor.shutdown(wait=True)
