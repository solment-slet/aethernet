import asyncio
import logging
import queue
import threading
import tkinter as tk

from PIL import Image, ImageTk

from aethernet.low_transports.tcp_low_transport import TCPLowTransport
from aethernet.stasis import AethernetStasisClient
from aethernet import get_link, ReliabilityMode


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("client")


class AethernetWorker:
    """
    Полностью владеет asyncio, Aethernet link и stasis.

    Этот объект работает в отдельном потоке.
    """

    def __init__(self, image_queue: queue.Queue):
        self.image_queue = image_queue

        self.thread = threading.Thread(
            target=self._thread_main,
            daemon=True,
        )

    def start(self):
        self.thread.start()

    def _thread_main(self):
        """
        Точка входа отдельного потока.
        Здесь создаётся и запускается свой asyncio event loop.
        """
        asyncio.run(self._async_main())

    async def _async_main(self):
        low_transport = await asyncio.to_thread(
            TCPLowTransport.connect,
            "127.0.0.1",
            8765,
        )

        # noinspection PyTypeChecker
        link = await get_link(
            low_transport,
            reliability_mode=ReliabilityMode.STOP_AND_WAIT,
            image_reliability_mode=ReliabilityMode.NONE,
            logger=logger,
        )

        try:
            stasis = AethernetStasisClient(link)

            server_metadata = await stasis.connect()
            logger.info("Connected: %s", server_metadata)

            await stasis.set_stream_mode(mode="interval", interval_ms=1000)

            # Бесконечное получение кадров.
            await self._receive_images(stasis)

        except Exception:
            logger.exception("Aethernet worker failed")

        finally:
            logger.warning("Closing link")
            await link.close()

    async def _receive_images(self, stasis: AethernetStasisClient):
        """
        Получает изображения одно за другим.

        Важный момент:

            image = await stasis.get_screen()

        может ждать бесконечно.

        Это совершенно нормально: данный поток будет ждать,
        но Tkinter находится в другом потоке и продолжает работать.
        """

        while True:
            try:
                image = await stasis.get_screen()

                if not isinstance(image, Image.Image):
                    logger.warning(
                        "get_screen() returned unexpected value: %r",
                        type(image),
                    )
                    continue

                # Если очередь уже содержит старый кадр,
                # удаляем его и оставляем только самый свежий.
                try:
                    self.image_queue.get_nowait()
                except queue.Empty:
                    pass

                self.image_queue.put_nowait(image)

            except Exception:
                logger.exception("Error while getting screen")

                # Если произошла ошибка, пытаемся получить
                # следующий кадр через секунду.
                await asyncio.sleep(1)


class TkinterViewer:
    """
    Tkinter должен полностью находиться в главном потоке.
    """

    def __init__(self, image_queue: queue.Queue):
        self.image_queue = image_queue

        self.root = tk.Tk()
        self.root.title("Aethernet Screen")
        self.root.configure(bg="black")
        self.root.geometry("500x300")

        # На старте label имеет чёрный фон,
        # поэтому до первого кадра окно полностью чёрное.
        self.label = tk.Label(
            self.root,
            bg="black",
        )
        self.label.pack(
            fill="both",
            expand=True,
        )

        # Ссылка на последний отображённый PIL Image.
        self.current_image: Image.Image | None = None

        # Ссылка на ImageTk.PhotoImage.
        #
        # Её обязательно нужно сохранять, иначе Python может
        # удалить объект, и изображение исчезнет из Tkinter.
        self.tk_image: ImageTk.PhotoImage | None = None

        # Запускаем проверку очереди.
        self.root.after(10, self._poll_image_queue)

        # Если окно закрывается — завершаем Tkinter.
        self.root.protocol(
            "WM_DELETE_WINDOW",
            self._on_close,
        )

    def _poll_image_queue(self):
        """
        Выполняется в главном потоке Tkinter.

        Если нового изображения нет — ничего не делаем.
        Поэтому последний кадр остаётся на экране сколько угодно.
        """

        try:
            # Забираем самый свежий кадр.
            image = self.image_queue.get_nowait()

            self.current_image = image

            self._display_image(image)

        except queue.Empty:
            # Нового кадра нет.
            #
            # НИЧЕГО НЕ ДЕЛАЕМ.
            #
            # Благодаря этому предыдущий кадр остаётся на экране.

            pass

        # Проверяем очередь снова.
        self.root.after(10, self._poll_image_queue)

    def _display_image(self, image: Image.Image):
        """
        Показывает PIL Image на весь размер окна.
        """

        width = self.label.winfo_width()
        height = self.label.winfo_height()

        # Во время самого первого запуска Tkinter может ещё
        # не успеть определить размеры виджета.
        if width <= 1 or height <= 1:
            return

        # Растягиваем изображение на весь Label.
        resized = image.resize(
            (width, height),
            Image.Resampling.LANCZOS,
        )

        self.tk_image = ImageTk.PhotoImage(resized)

        self.label.configure(
            image=self.tk_image,
        )

    def _on_close(self):
        """
        Закрытие окна.
        """

        self.root.destroy()

    def run(self):
        """
        Запускает главный цикл Tkinter.
        """

        self.root.mainloop()


def main():
    # Потокобезопасная очередь для передачи кадров:
    #
    # asyncio thread
    #       │
    #       │ PIL.Image.Image
    #       ▼
    #    Queue
    #       │
    #       ▼
    # Tkinter main thread
    image_queue = queue.Queue(maxsize=1)

    # Весь Aethernet запускается в отдельном потоке.
    worker = AethernetWorker(image_queue)
    worker.start()

    # Tkinter остаётся в главном потоке.
    viewer = TkinterViewer(image_queue)
    viewer.run()


if __name__ == "__main__":
    main()