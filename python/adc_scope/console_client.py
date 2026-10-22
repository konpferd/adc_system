"""!
@file console_client.py
@brief TCP client for the device control console (port 5000).

Commands are queued by the GUI and executed one at a time: a command is sent, then every
line received until the next prompt "> " belongs to its reply (including broadcast log
lines such as the DISPLAY_SETTINGS report). All results are delivered as ConsoleEvent
objects through a thread-safe queue that the GUI polls, so no Qt objects are touched from
the network thread.
"""
from __future__ import annotations

import queue
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional

from adc_common.console_protocol import (BANNER_DONE_TEXT, ConsoleTokenizer, InitReport, LineKind,
                                         classify_line, parse_init_line)

from . import config


@dataclass
class ConsoleReply:
    """!
    @brief Result of one executed command.
    """
    command: str
    tag: Optional[str] = None
    lines: List[str] = field(default_factory=list)
    ok: bool = False
    timed_out: bool = False
    error: Optional[str] = None             #!< first ERR / fault line, if any
    quiet: bool = False                     #!< background poll: keep out of the console view

    @property
    def status_line(self) -> str:
        """!
        @brief The final OK/ERR line of the reply.
        @return Status line or "" when none was received.
        """
        for line in reversed(self.lines):
            if line.startswith(("OK", "ERR")):
                return line
        return ""


@dataclass
class ConsoleEvent:
    """!
    @brief Message from the console thread to the GUI.

    kind is one of: "connected", "disconnected", "banner", "line", "tx", "reply", "status".
    """
    kind: str
    text: str = ""
    line_kind: LineKind = LineKind.INFO
    in_reply: bool = False                  #!< line belongs to a command reply
    quiet: bool = False                     #!< part of a background poll (hide in the console view)
    reply: Optional[ConsoleReply] = None
    init: Optional[InitReport] = None


class ConsoleClient:
    """!
    @brief Background console connection with a command queue.
    """

    def __init__(self) -> None:
        """!
        @brief Create an idle client.
        """
        self.host: str = ""
        self.port: int = 0
        self.events: "queue.Queue[ConsoleEvent]" = queue.Queue()
        self.connected: bool = False
        self._cmds: Deque[tuple] = deque()
        self._cmd_lock: threading.Lock = threading.Lock()
        self._stop: threading.Event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None

    # ---- control -------------------------------------------------------------------------
    def start(self, host: str, port: int) -> None:
        """!
        @brief Connect (and keep reconnecting) to host:port.
        @param host  Device address.
        @param port  Console port.
        """
        self.stop()
        self.host, self.port = host, port
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="console", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """!
        @brief Disconnect and stop the thread; queued commands are discarded.
        """
        self._stop.set()
        s: Optional[socket.socket] = self._sock
        if s is not None:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        with self._cmd_lock:
            self._cmds.clear()
        self.connected = False

    def send(self, command: str, tag: Optional[str] = None, quiet: bool = False) -> bool:
        """!
        @brief Queue a command for execution.
        @param command  Command text without terminator.
        @param tag      Optional identifier returned with the reply.
        @param quiet    Background poll: its echo and status line are flagged so the GUI can
                        keep them out of the console view (faults are never hidden).
        @return False when not connected (command dropped).
        """
        if not self.connected:
            return False
        with self._cmd_lock:
            self._cmds.append((command.strip(), tag, quiet))
        return True

    def pending_commands(self) -> int:
        """!
        @brief Number of queued commands not yet sent.
        @return Queue length.
        """
        with self._cmd_lock:
            return len(self._cmds)

    def poll(self, max_events: int = 1000) -> List[ConsoleEvent]:
        """!
        @brief Fetch queued events (GUI thread).
        @param max_events  Upper bound per call.
        @return Events in arrival order.
        """
        out: List[ConsoleEvent] = []
        try:
            while len(out) < max_events:
                out.append(self.events.get_nowait())
        except queue.Empty:
            pass
        return out

    # ---- thread ----------------------------------------------------------------------------
    def _emit(self, ev: ConsoleEvent) -> None:
        """!
        @brief Push an event to the GUI queue.
        @param ev  Event.
        """
        self.events.put(ev)

    def _run(self) -> None:
        """!
        @brief Thread body: connect / serve / reconnect until stopped.
        """
        while not self._stop.is_set():
            try:
                sock: socket.socket = socket.create_connection((self.host, self.port),
                                                               timeout=config.CONNECT_TIMEOUT_S)
            except OSError as e:
                self._emit(ConsoleEvent("status", f"console {self.host}:{self.port} unreachable: {e}"))
                self._stop.wait(config.RECONNECT_INTERVAL_S * 2)
                continue
            sock.settimeout(0.05)
            self._sock = sock
            self.connected = True
            self._emit(ConsoleEvent("connected", f"{self.host}:{self.port}"))
            try:
                self._serve(sock)
            except (OSError, ConnectionError) as e:
                if not self._stop.is_set():
                    self._emit(ConsoleEvent("status", f"console connection lost: {e}"))
            finally:
                self.connected = False
                self._sock = None
                try:
                    sock.close()
                except OSError:
                    pass
                with self._cmd_lock:
                    self._cmds.clear()
                self._emit(ConsoleEvent("disconnected"))
            if not self._stop.is_set():
                self._stop.wait(config.RECONNECT_INTERVAL_S * 2)

    def _serve(self, sock: socket.socket) -> None:
        """!
        @brief Receive/transmit loop of one connection.
        @param sock  Connected socket.
        @throws ConnectionError when the device closes the connection.
        """
        tok: ConsoleTokenizer = ConsoleTokenizer()
        init: InitReport = InitReport()
        banner_done: bool = False
        banner_prompt_due: bool = False          # the banner ends with a prompt that is not a reply
        t_banner: float = 0.0
        t_connect: float = time.monotonic()
        current: Optional[ConsoleReply] = None
        t_sent: float = 0.0
        while not self._stop.is_set():
            # ---- transmit the next command when idle
            if banner_prompt_due and time.monotonic() - t_banner > 0.3:
                banner_prompt_due = False
            if current is None and not banner_prompt_due and (
                    banner_done or time.monotonic() - t_connect > config.CONSOLE_BANNER_TIMEOUT_S):
                if not banner_done:
                    banner_done = True
                    self._emit(ConsoleEvent("banner", init=init))
                item: Optional[tuple] = None
                with self._cmd_lock:
                    if self._cmds:
                        item = self._cmds.popleft()
                if item is not None:
                    cmd, tag, quiet = item
                    current = ConsoleReply(cmd, tag, quiet=quiet)
                    sock.sendall((cmd + "\r\n").encode("latin-1"))
                    t_sent = time.monotonic()
                    self._emit(ConsoleEvent("tx", cmd, quiet=quiet))
            # ---- receive
            try:
                data: bytes = sock.recv(65536)
            except socket.timeout:
                data = b""
                if current is not None and time.monotonic() - t_sent > config.CONSOLE_REPLY_TIMEOUT_S:
                    current.timed_out = True
                    current.error = current.error or "no reply (timeout)"
                    self._emit(ConsoleEvent("reply", reply=current))
                    current = None
                continue
            if not data:
                raise ConnectionError("closed by device")
            for kind, text in tok.feed(data):
                if kind == "prompt":
                    if current is None and banner_prompt_due:
                        banner_prompt_due = False
                    elif current is not None:
                        self._finish(current)
                        current = None
                    continue
                lk: LineKind = classify_line(text)
                if not banner_done:
                    parse_init_line(text, init)
                    if text.strip() == BANNER_DONE_TEXT:
                        banner_done = True
                        banner_prompt_due = True
                        t_banner = time.monotonic()
                        self._emit(ConsoleEvent("line", text, lk))
                        self._emit(ConsoleEvent("banner", init=init))
                        continue
                if current is not None and current.quiet and lk not in (LineKind.OK, LineKind.ERR):
                    # an asynchronous log line during a background poll: never swallow it
                    self._emit(ConsoleEvent("line", text, lk))
                    continue
                if current is not None:
                    current.lines.append(text)
                    if lk in (LineKind.ERR, LineKind.FAULT) and current.error is None:
                        current.error = text.strip()
                self._emit(ConsoleEvent("line", text, lk, in_reply=current is not None,
                                        quiet=current is not None and current.quiet))

    def _finish(self, reply: ConsoleReply) -> None:
        """!
        @brief Complete a reply at the prompt and publish it.
        @param reply  Reply collected so far.
        """
        status: str = reply.status_line
        reply.ok = reply.error is None and (status.startswith("OK") or (status == "" and bool(reply.lines)))
        if status.startswith("ERR") and reply.error is None:
            reply.error = status
        self._emit(ConsoleEvent("reply", reply=reply))
