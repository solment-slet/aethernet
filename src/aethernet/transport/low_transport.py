import random
import time

import vk_api
from vk_api.longpoll import VkLongPoll, VkEventType


class LowTransport:
    """Отправка/получение сообщений через VK API."""

    def __init__(
        self,
        vk_api_version: str,
        vk_token: str,
        vk_receiver_id: int,
    ) -> None:
        self.receiver_id = vk_receiver_id
        self._session = vk_api.VkApi(token=vk_token, api_version=vk_api_version)
        self._api = self._session.get_api()

    def send_message(self, text: str) -> None:
        """Синхронная отправка сообщения."""
        self._api.messages.send(
            peer_id=self.receiver_id,
            message=text,
            random_id=random.randint(1, 2**31),
        )

    def wait_for_message(
        self,
        timeout: float | None = None,
    ) -> str | None:
        """
        Синхронно ждёт сообщение от from_id через VK Long Poll.

        Если prefix задан, ждёт только сообщение, начинающееся с этого префикса.
        """
        longpoll = VkLongPoll(self._session)
        deadline = time.time() + timeout if timeout is not None else None
        print("Начинаем слушать сообщения")
        for event in longpoll.listen():
            if timeout is not None and time.time() > deadline:
                return None

            if event.type == VkEventType.MESSAGE_NEW and not event.from_me:
                print("Получено сообщение!")
                if (
                    getattr(event, "user_id", None) == self.receiver_id
                    or event.peer_id == self.receiver_id
                ):
                    return event.text

        return None
