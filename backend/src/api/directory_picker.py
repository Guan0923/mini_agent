"""Serialized native folder selection for the local desktop backend."""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi import Request

_picker_lock = threading.Lock()


class DirectoryPickerBusyError(RuntimeError):
    """Raised when another local folder picker is already open."""


def pick_directory(request: Request, *, title: str) -> Path | None:
    if not _picker_lock.acquire(blocking=False):
        raise DirectoryPickerBusyError("已有一个文件夹选择窗口正在打开。")
    try:
        injected = getattr(request.app.state.web, "project_picker", None)
        if injected is not None:
            try:
                selected = injected()
                if selected is None or not str(selected):
                    return None
                return Path(selected)
            except OSError:
                raise
            except Exception as exc:
                raise OSError("当前环境无法打开系统文件夹选择器。") from exc
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            try:
                root.withdraw()
                root.attributes("-topmost", True)
                selected = filedialog.askdirectory(title=title, mustexist=True)
            finally:
                root.destroy()
        except Exception as exc:
            raise OSError("当前环境无法打开系统文件夹选择器。") from exc
        return Path(selected) if selected else None
    finally:
        _picker_lock.release()
