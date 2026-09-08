"""Bounded binary transfers; upload publishes only after the final chunk."""
import base64
import os
from pathlib import Path
import stat
import tempfile

from pydantic import BaseModel, ConfigDict, Field

CHUNK_BYTES = 262_144
MAX_BINARY_BYTES = 52_428_800


class ReadBytes(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    file_path: str
    offset: int = Field(default=0, ge=0, le=MAX_BINARY_BYTES)
    version: str | None = None


class WriteBytes(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    file_path: str
    transfer_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    offset: int = Field(ge=0, le=MAX_BINARY_BYTES)
    content: str = Field(max_length=349528)
    final: bool


class DeviceFiles:
    def __init__(self, backend):
        self.backend = backend
        self.uploads = {}

    def read(self, **arguments):
        args = ReadBytes(**arguments)
        path = self.backend._resolve_path(args.file_path)
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BINARY_BYTES:
                raise ValueError("unsupported file")
            version = f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}"
            if args.version is not None and version != args.version:
                raise ValueError("file changed during transfer")
            source.seek(args.offset)
            data = source.read(CHUNK_BYTES)
        return {"content": base64.b64encode(data).decode(), "version": version,
                "final": args.offset + len(data) >= info.st_size}

    def write(self, **arguments):
        args = WriteBytes(**arguments)
        data = base64.b64decode(args.content, validate=True)
        if len(data) > CHUNK_BYTES or args.offset + len(data) > MAX_BINARY_BYTES:
            raise ValueError("file too large")
        target = self.backend._resolve_path(args.file_path)
        entry = self.uploads.get(args.transfer_id)
        if entry is None:
            if args.offset or len(self.uploads) >= 4:
                raise ValueError("invalid upload or too many uploads")
            target.parent.mkdir(parents=True, exist_ok=True)
            file = tempfile.NamedTemporaryFile(prefix=".assistant-upload-", dir=target.parent, delete=False)
            entry = (target, file)
            self.uploads[args.transfer_id] = entry
        path, file = entry
        if path != target or file.tell() != args.offset:
            raise ValueError("upload position mismatch")
        file.write(data)
        if args.final:
            file.flush()
            os.fsync(file.fileno())
            file.close()
            os.replace(file.name, target)
            self.uploads.pop(args.transfer_id)
        return {"path": args.file_path}

    def close(self):
        for _, file in self.uploads.values():
            file.close()
            Path(file.name).unlink(missing_ok=True)
        self.uploads.clear()

    def abort(self, transfer_id):
        entry = self.uploads.pop(transfer_id, None)
        if entry:
            entry[1].close()
            Path(entry[1].name).unlink(missing_ok=True)
        return {}
