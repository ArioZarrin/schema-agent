"""Bounded DuckDB lock recovery for the project's notebook connections."""
import os
from pathlib import Path
import queue
import re
import shlex
import subprocess
import time

import duckdb


def close_database(connection):
    """Closing an already-closed connection is harmless."""
    if connection is not None:
        try:
            connection.close()
        except Exception:
            pass


def _release_idle_notebook(path, error, log):
    match = re.search(r"PID\s+(\d+)", str(error))
    if not match or int(match[1]) == os.getpid():
        return False
    client = None
    try:
        process = subprocess.run(
            ["ps", "-p", match[1], "-o", "command="],
            capture_output=True, text=True, check=True, timeout=3,
        )
        arguments = shlex.split(process.stdout)
        if "ipykernel_launcher" not in arguments:
            return False
        connection_file = None
        for index, argument in enumerate(arguments):
            if argument.startswith(("--f=", "-f=")):
                connection_file = argument.split("=", 1)[1]
            elif argument in {"-f", "--f"} and index + 1 < len(arguments):
                connection_file = arguments[index + 1]
        if not connection_file or not Path(connection_file).is_file():
            return False
        from jupyter_client import BlockingKernelClient
        client = BlockingKernelClient(connection_file=connection_file)
        client.load_connection_file()
        client.start_channels()
        # A busy kernel is left alone. No interrupt, shutdown or process kill.
        client.wait_for_ready(timeout=3)
        code = f'''
from pathlib import Path as _firmable_lock_Path
import duckdb as _firmable_lock_duckdb
_firmable_lock_target = _firmable_lock_Path({str(path.resolve())!r})
_firmable_lock_pending = False
if globals().get('workflow') is not None and globals().get('ACTIVE_THREAD_ID'):
    _firmable_lock_pending = any(task.interrupts for task in workflow.get_state({{'configurable': {{'thread_id': ACTIVE_THREAD_ID}}}}).tasks)
if _firmable_lock_pending:
    print('FIRMABLE_WORKFLOW_PENDING')
else:
    _firmable_lock_released = False
    for _firmable_lock_name in ('con', 'db', 'part3_con', 'part4_con'):
        _firmable_lock_connection = globals().get(_firmable_lock_name)
        if not isinstance(_firmable_lock_connection, _firmable_lock_duckdb.DuckDBPyConnection):
            continue
        try:
            _firmable_lock_files = _firmable_lock_connection.execute('PRAGMA database_list').fetchall()
            if any(row[2] and _firmable_lock_Path(row[2]).resolve() == _firmable_lock_target for row in _firmable_lock_files):
                _firmable_lock_connection.close()
                globals()[_firmable_lock_name] = None
                _firmable_lock_released = True
        except _firmable_lock_duckdb.Error:
            pass
    if _firmable_lock_released:
        print('FIRMABLE_CONNECTION_RELEASED')
'''
        message_id = client.execute(code, store_history=False, allow_stdin=False, stop_on_error=True)
        deadline = time.monotonic() + 5
        released = False
        while time.monotonic() < deadline:
            try:
                message = client.get_iopub_msg(timeout=.5)
            except queue.Empty:
                continue
            if message.get("parent_header", {}).get("msg_id") != message_id:
                continue
            kind = message["header"]["msg_type"]
            if kind == "stream":
                text = message["content"]["text"]
                released |= "FIRMABLE_CONNECTION_RELEASED" in text
                if "FIRMABLE_WORKFLOW_PENDING" in text:
                    log("Database: another notebook has a pending workflow; its connection was preserved.")
            elif kind == "error":
                return False
            elif kind == "status" and message["content"]["execution_state"] == "idle":
                if released:
                    log(f"Database: released an idle notebook connection (PID {match[1]}).")
                return released
        return False
    except Exception:
        # Missing local kernel access, a busy owner or a non-Jupyter process.
        return False
    finally:
        if client is not None:
            try:
                client.stop_channels()
            except Exception:
                pass


def connect_database(path, log=print):
    """Return a connection, or None with a readable status; never a lock traceback."""
    path = Path(path)
    try:
        # All project connections use the same mode, including read-only queries.
        # This also avoids incompatible settings within a shared notebook kernel.
        return duckdb.connect(str(path))
    except duckdb.Error as error:
        if "lock" in str(error).lower():
            _release_idle_notebook(path, error, log)
            try:
                return duckdb.connect(str(path))
            except duckdb.Error:
                log("Database: still in use. Use available exported files or pause this run; existing results are preserved.")
                return None
        log(f"Database: could not open {path.name}: {error}")
        return None
