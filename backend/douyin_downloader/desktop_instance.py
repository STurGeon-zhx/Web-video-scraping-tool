from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket


class DesktopInstance(QObject):
    """Coordinate one desktop process and activate it from later launches."""

    activation_requested = Signal()

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name
        self._server: QLocalServer | None = None
        self._connections: set[QLocalSocket] = set()

    def acquire(self) -> bool:
        if self._server is not None and self._server.isListening():
            return True

        if self._notify_existing_instance():
            return False

        QLocalServer.removeServer(self.name)
        server = QLocalServer(self)
        server.newConnection.connect(self._accept_connections)
        if server.listen(self.name):
            self._server = server
            return True

        server.deleteLater()
        raise RuntimeError(f"无法创建单实例通信服务：{server.errorString()}")

    def _notify_existing_instance(self) -> bool:
        socket = QLocalSocket()
        socket.connectToServer(self.name)
        if not socket.waitForConnected(1000):
            socket.abort()
            return False
        socket.write(b"activate")
        socket.flush()
        socket.waitForBytesWritten(1000)
        QCoreApplication.processEvents()
        socket.disconnectFromServer()
        return True

    def _accept_connections(self) -> None:
        if self._server is None:
            return
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                continue
            self._connections.add(socket)
            socket.readyRead.connect(lambda current=socket: self._read(current))
            socket.disconnected.connect(
                lambda current=socket: self._discard(current)
            )
            self._read(socket)

    def _read(self, socket: QLocalSocket) -> None:
        message = bytes(socket.readAll())
        if b"activate" in message:
            self.activation_requested.emit()

    def _discard(self, socket: QLocalSocket) -> None:
        self._connections.discard(socket)
        socket.deleteLater()

    def close(self) -> None:
        for socket in tuple(self._connections):
            socket.abort()
            socket.deleteLater()
        self._connections.clear()
        if self._server is None:
            return
        self._server.close()
        QLocalServer.removeServer(self.name)
        self._server.deleteLater()
        self._server = None
