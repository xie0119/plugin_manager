"""Read an MSI database and expand its embedded CABs, without installer services."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from functools import lru_cache
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


class Database:
    def __init__(self, path: Path):
        self.api = ctypes.WinDLL("msi", use_last_error=True)
        api = self.api
        handle = ctypes.c_uint
        pointer = ctypes.POINTER
        api.MsiOpenDatabaseW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, pointer(handle)]
        api.MsiDatabaseOpenViewW.argtypes = [handle, ctypes.c_wchar_p, pointer(handle)]
        api.MsiViewExecute.argtypes = [handle, handle]
        api.MsiViewFetch.argtypes = [handle, pointer(handle)]
        api.MsiRecordGetStringW.argtypes = [handle, ctypes.c_uint, ctypes.c_wchar_p, pointer(ctypes.c_uint)]
        api.MsiRecordReadStream.argtypes = [handle, ctypes.c_uint, ctypes.c_void_p, pointer(ctypes.c_uint)]
        api.MsiCloseHandle.argtypes = [handle]
        self.handle = handle()
        self.check(api.MsiOpenDatabaseW(str(path), None, ctypes.byref(self.handle)))

    @staticmethod
    def check(result):
        if result:
            raise OSError(result, "读取 MSI 数据库失败")

    @contextmanager
    def records(self, sql):
        view = ctypes.c_uint()
        self.check(self.api.MsiDatabaseOpenViewW(self.handle, sql, ctypes.byref(view)))
        self.check(self.api.MsiViewExecute(view, 0))

        def rows():
            while True:
                record = ctypes.c_uint()
                code = self.api.MsiViewFetch(view, ctypes.byref(record))
                if code == 259:
                    return
                self.check(code)
                try:
                    yield record
                finally:
                    self.api.MsiCloseHandle(record)
        try:
            yield rows()
        finally:
            self.api.MsiCloseHandle(view)

    def string(self, record, column):
        size = ctypes.c_uint(4096)
        buffer = ctypes.create_unicode_buffer(size.value)
        code = self.api.MsiRecordGetStringW(record, column, buffer, ctypes.byref(size))
        if code == 234:
            size.value += 1
            buffer = ctypes.create_unicode_buffer(size.value)
            code = self.api.MsiRecordGetStringW(record, column, buffer, ctypes.byref(size))
        self.check(code)
        return buffer.value

    def query(self, sql, columns):
        with self.records(sql) as records:
            return [tuple(self.string(row, col) for col in range(1, columns + 1)) for row in records]

    def stream(self, name, output):
        sql = "SELECT `Data` FROM `_Streams` WHERE `Name`='" + name.replace("'", "''") + "'"
        with self.records(sql) as records, output.open("wb") as target:
            record = next(records)
            while True:
                buffer = ctypes.create_string_buffer(65536)
                size = ctypes.c_uint(len(buffer))
                self.check(self.api.MsiRecordReadStream(record, 1, buffer, ctypes.byref(size)))
                if not size.value:
                    break
                target.write(buffer.raw[:size.value])

    def close(self):
        self.api.MsiCloseHandle(self.handle)


def extract_msi(package: Path, destination: Path, work: Path) -> None:
    work.mkdir(parents=True, exist_ok=True)
    db = Database(package)
    try:
        directories = {key: (parent, name) for key, parent, name in db.query(
            "SELECT `Directory`, `Directory_Parent`, `DefaultDir` FROM `Directory`", 3)}
        components = dict(db.query("SELECT `Component`, `Directory_` FROM `Component`", 2))
        files = db.query("SELECT `File`, `FileName`, `Component_` FROM `File`", 3)
        cabinets = db.query("SELECT `Cabinet` FROM `Media`", 1)

        @lru_cache(None)
        def directory(key):
            if not key or key == "TARGETDIR":
                return Path()
            parent, name = directories[key]
            name = name.split(":", 1)[0].split("|")[-1]
            return directory(parent) / ("" if name == "." else name)

        with tempfile.TemporaryDirectory(prefix="msi-files-", dir=work) as temporary:
            temporary = Path(temporary)
            extracted = temporary / "files"
            extracted.mkdir()
            for index, (name,) in enumerate(cabinets):
                if not name.startswith("#"):
                    raise ValueError("只支持包含嵌入 CAB 的官方 MSI")
                cab = temporary / f"payload-{index}.cab"
                db.stream(name[1:], cab)
                process = subprocess.run([str(Path(os.environ["SystemRoot"]) / "System32/expand.exe"),
                                          "-F:*", str(cab), str(extracted)],
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         creationflags=subprocess.CREATE_NO_WINDOW)
                if process.returncode:
                    raise RuntimeError(f"CAB 提取失败：{process.stdout!r}")
            for identifier, name, component in files:
                relative = directory(components[component]) / name.split("|")[-1]
                output = destination / relative
                if not output.resolve().is_relative_to(destination.resolve()):
                    raise ValueError("MSI 文件路径越界")
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(extracted / identifier, output)
    finally:
        db.close()
