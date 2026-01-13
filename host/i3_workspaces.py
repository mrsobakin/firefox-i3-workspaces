#!/usr/bin/env python3

import asyncio
import json
import logging
import re
import struct
import sys
from typing import Any, Dict, Optional

from i3ipc.aio import Connection
from i3ipc.events import Event, IpcBaseEvent, WindowEvent, WorkspaceEvent

LOG_FILE = '/tmp/i3_workspaces.log'
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)

class Bridge:
    def __init__(self):
        self.i3: Optional[Connection] = None
        self.tracked_windows: Dict[int, str] = {}
        self.known_workspaces: Dict[int, str] = {}

    async def start(self):
        try:
            self.i3 = await Connection(auto_reconnect=True).connect()
            logging.info("Connected to i3")

            tree = await self.i3.get_tree()
            self.known_workspaces = {ws.id: ws.name for ws in tree.workspaces()}

            self.i3.on(Event.WINDOW_MOVE, self.on_window_move)
            self.i3.on(Event.WORKSPACE_RENAME, self.on_workspace_rename)

            await asyncio.gather(self.i3.main(), self.native_messaging_loop())

        except Exception:
            logging.exception("Fatal error in main loop")
        finally:
            if self.i3:
                self.i3.main_quit()

    async def send_json(self, msg: Any):
        try:
            logging.info('→ %s', msg)
            encoded = json.dumps(msg, separators=(',', ':')).encode('utf-8')
            sys.stdout.buffer.write(struct.pack('@I', len(encoded)))
            sys.stdout.buffer.write(encoded)
            sys.stdout.buffer.flush()
        except Exception:
            logging.exception("Failed to send message")

    async def native_messaging_loop(self):
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)

        while True:
            header = await reader.readexactly(4)
            if not header:
                break

            msg_len = struct.unpack('@I', header)[0]
            msg_raw = await reader.readexactly(msg_len)
            message = json.loads(msg_raw)
            logging.info('← %s', message)

            if 'windows' in message:
                await self.sync_windows(message['windows'])

    async def sync_windows(self, windows: Dict[str, Optional[str]]):
        tree = await self.i3.get_tree()
        response: Dict[str, str] = {}

        for uuid, target_workspace in windows.items():
            cons = tree.find_named(fr'^{re.escape(uuid)} \|')

            if not cons:
                logging.warning(f'{uuid} not found')
                continue

            con = cons[0]
            self.tracked_windows[con.id] = uuid

            if target_workspace:
                await con.command(f'move --no-auto-back-and-forth container to workspace "{target_workspace}"')
                response[uuid] = target_workspace
            else:
                response[uuid] = con.workspace().name

        await self.send_json({'windows': response})

    async def on_window_move(self, _: Connection, e: IpcBaseEvent):
        assert isinstance(e, WindowEvent)

        uuid = self.tracked_windows.get(e.container.id)
        if not uuid:
            return

        tree = await self.i3.get_tree()
        con = tree.find_by_id(e.container.id)

        if con and con.workspace():
            await self.send_json({'window::move': {uuid: con.workspace().name}})

    async def on_workspace_rename(self, _: Connection, e: IpcBaseEvent):
        assert isinstance(e, WorkspaceEvent)

        old_name = self.known_workspaces.get(e.current.id)
        self.known_workspaces[e.current.id] = e.current.name

        if old_name and old_name != e.current.name:
            await self.send_json({'workspace::rename': {old_name: e.current.name}})

if __name__ == '__main__':
    asyncio.run(Bridge().start())
