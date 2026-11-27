"""Own BLE loop shutdown and release packaged Bleak 0.22 WinRT resources."""
import asyncio
import threading


class BleLoopOwner:
    """Keep the loop alive until cleanup finishes; reject callbacks after exit."""
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._dispatch_lock = threading.RLock()
        self._accepting = True
        dispatch = self.loop.call_soon_threadsafe

        def call_soon_threadsafe(callback, *args, **kwargs):
            # Removing a WinRT event token cannot recall a callback that Windows
            # already dispatched. Serialize the final close with those posts.
            with self._dispatch_lock:
                if not self._accepting or self.loop.is_closed():
                    return None
                return dispatch(callback, *args, **kwargs)

        self.loop.call_soon_threadsafe = call_soon_threadsafe
        self.thread = threading.Thread(target=self._run, name='ble-loop', daemon=True)
        self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_forever()
        finally:
            with self._dispatch_lock:
                self._accepting = False
                self.loop.close()

    def stop(self, _future=None):
        self.loop.call_soon_threadsafe(self.loop.stop)


def release_winrt_resources(client, notify_characteristic=None, on_error=None):
    """Finish disposal when WinRT disconnect times out before its CLOSED event.

    This adapter is limited to the WinRT backend shipped with the tool. Never
    change system Bleak files or assume other backends share private fields.
    """
    backend = getattr(client, '_backend', None)
    if backend is None or type(backend).__module__ != 'bleak.backends.winrt.client':
        return

    def attempt(label, action):
        try:
            action()
        except Exception as error:
            if on_error:
                on_error('%s：%s' % (label, str(error) or type(error).__name__))

    requester = getattr(backend, '_requester', None)
    session = getattr(backend, '_session', None)
    services = getattr(backend, 'services', None)

    def remove_token(owner, token_name, remover):
        token = getattr(backend, token_name, None)
        if owner is not None and token is not None:
            attempt('注销 ' + token_name, lambda: getattr(owner, remover)(token))
        setattr(backend, token_name, None)

    remove_token(requester, '_services_changed_token', 'remove_gatt_services_changed')
    remove_token(session, '_session_status_changed_token', 'remove_session_status_changed')
    remove_token(session, '_max_pdu_size_changed_token', 'remove_max_pdu_size_changed')
    callbacks = getattr(backend, '_notification_callbacks', {})
    for handle, token in list(callbacks.items()):
        def remove_notification(handle=handle, token=token):
            char = services.get_characteristic(handle) if services is not None else None
            if char is None and getattr(notify_characteristic, 'handle', None) == handle:
                char = notify_characteristic
            if char is not None:
                char.obj.remove_value_changed(token)
        attempt('注销通知回调', remove_notification)
    callbacks.clear()

    # Detach references before Close can queue another Windows status callback.
    backend._requester = None
    backend._session = None
    backend.services = None
    if session is not None:
        attempt('取消保持连接', lambda: setattr(session, 'maintain_connection', False))
    if services is not None:
        for service in services:
            attempt('释放 GATT 服务', lambda service=service: service.obj.close())
    if requester is not None:
        attempt('释放蓝牙设备', requester.close)
    if session is not None:
        attempt('释放 GATT 会话', session.close)
