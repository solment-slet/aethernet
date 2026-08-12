import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import mss
from PIL import Image
from screeninfo import get_monitors

from aethernet.typing import LoggerLike


class ScreenManager:
    def __init__(self, logger: LoggerLike | None = None):
        monitors = get_monitors()
        self.monitor = next(
            (m for m in monitors if m.is_primary), monitors[0] if monitors else None
        )
        if self.monitor is None:
            raise RuntimeError("No matching monitor found")
        self.width = self.monitor.width
        self.height = self.monitor.height
        self.x = self.monitor.x
        self.y = self.monitor.y
        self.monitor_info = {
            "left": self.x,
            "top": self.y,
            "width": self.width,
            "height": self.height,
        }

        self._logger = logger if logger is not None else logging.getLogger(__name__)
        self._executor = ThreadPoolExecutor(max_workers=1)
        # mss не потокобезопасен между потоками: экземпляр должен создаваться
        # и использоваться в том же потоке. Так как executor однопоточный
        # (max_workers=1), достаточно ленивой инициализации через thread-local.
        self._thread_local = threading.local()

    def _get_sct(self) -> mss.base.MSSBase:
        sct = getattr(self._thread_local, "sct", None)
        if sct is None:
            sct = mss.mss()
            self._thread_local.sct = sct
        return sct

    def _grab_sync(self) -> Image.Image:
        sct = self._get_sct()
        shot = sct.grab(self.monitor_info)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    async def screenshot(self) -> Image.Image:
        loop = asyncio.get_running_loop()
        try:
            # noinspection PyTypeChecker
            img = await asyncio.wait_for(
                loop.run_in_executor(self._executor, self._grab_sync),
                timeout=5.0,
            )
        except TimeoutError:
            self._logger.error("[screen] grab timed out")
            raise
        return img

    async def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
