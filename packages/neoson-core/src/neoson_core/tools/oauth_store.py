"""Cache local atômico: DPAPI por usuário no Windows, arquivo0600 no POSIX."""

import ctypes
import json
import os
import stat
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from neoson_core.engine.integrations import IntegrationError


@dataclass(frozen=True, slots=True)
class OAuthTokens:
    client_id: str
    tenant: str
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: float
    scopes: tuple[str, ...]


class _Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(value: bytes, *, decrypt: bool = False) -> bytes:
    if sys.platform != "win32":
        raise IntegrationError("credentials")
    # Sem CRYPTPROTECT_LOCAL_MACHINE: só a identidade Windows atual pode abrir.
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
    source = _Blob(len(value), buffer)
    result = _Blob()
    method = library.CryptUnprotectData if decrypt else library.CryptProtectData
    method.argtypes = [
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(_Blob),
    ]
    method.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not method(
        ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)
    ):
        raise IntegrationError("credentials")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


class LocalTokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _check_path(self) -> None:
        # Recusar links e junctions em todos os componentes; diretório é do operador.
        for path in (self.path, *self.path.parents):
            if path.is_symlink() or path.is_junction():
                raise IntegrationError("credentials")

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        """Lock de SO, liberado inclusive após queda; não aguardar bloqueando o loop."""
        descriptor: int | None = None
        try:
            self._check_path()
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            lock = self.path.with_suffix(self.path.suffix + ".lock")
            LocalTokenStore(lock)._check_path()
            descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if descriptor is not None:
                os.close(descriptor)
            raise IntegrationError("credentials_busy") from None
        try:
            yield
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def load(self) -> OAuthTokens:
        try:
            self._check_path()
            metadata = self.path.stat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 131072:
                raise IntegrationError("credentials")
            if sys.platform != "win32" and (
                stat.S_IMODE(metadata.st_mode) & 0o077 or metadata.st_uid != os.getuid()
            ):
                raise IntegrationError("credentials")
            data = self.path.read_bytes()
            if os.name == "nt":
                if not data.startswith(b"DPAPI1\n"):
                    raise IntegrationError("credentials")
                data = _dpapi(data[7:], decrypt=True)
            body = json.loads(data)
            return OAuthTokens(
                client_id=body["client_id"],
                tenant=body["tenant"],
                access_token=body["access_token"],
                refresh_token=body["refresh_token"],
                expires_at=float(body["expires_at"]),
                scopes=tuple(body["scopes"]),
            )
        except (OSError, ValueError, KeyError, TypeError):
            raise IntegrationError("credentials") from None

    def save(self, tokens: OAuthTokens) -> None:
        temp: Path | None = None
        try:
            self._check_path()
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            data = json.dumps(asdict(tokens)).encode()
            if os.name == "nt":
                data = b"DPAPI1\n" + _dpapi(data)
            descriptor, name = tempfile.mkstemp(dir=self.path.parent, prefix=".oauth-")
            temp = Path(name)
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            temp.replace(self.path)
        except OSError:
            raise IntegrationError("credentials") from None
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
