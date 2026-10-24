from threading import Event

_wake_event = Event()

def wake_worker() -> None:
    _wake_event.set()

def wait_for_work(timeout: float | None) -> None:
    _wake_event.wait(timeout)
    _wake_event.clear()
