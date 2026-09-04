from abc import ABC, abstractmethod


class BaseMemory(ABC):
    def __init__(self, user_id: str) -> None:
        self._user_id = user_id

    @abstractmethod
    async def add(self, *args, **kwargs) -> None:
        pass

    @abstractmethod
    def retrieve(self, *args, **kwargs):
        pass

    @abstractmethod
    def clear(self, *args, **kwargs) -> None:
        pass
