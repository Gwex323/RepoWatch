"""Windows Credential Manager；没有明文文件或环境变量回退。"""

import ctypes
import os
from contextlib import contextmanager
from ctypes import wintypes
from hashlib import sha256

from repowatch.application.credentials import CredentialError, SecretValue, validate_reference
from repowatch.security import DEFAULT_REDACTOR, Redactor


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class WindowsCredentialVault:
    def __init__(self, namespace, redactor: Redactor = DEFAULT_REDACTOR):
        if os.name != "nt":
            raise CredentialError()
        validate_reference(namespace)
        self.prefix, self.redactor = "RepoWatch/" + namespace + "/", redactor
        self.api = ctypes.WinDLL("Advapi32", use_last_error=True)
        self.api.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_Credential)),
        ]
        self.api.CredReadW.restype = wintypes.BOOL
        self.api.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
        self.api.CredWriteW.restype = wintypes.BOOL
        self.api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self.api.CredDeleteW.restype = wintypes.BOOL
        self.api.CredFree.argtypes = [ctypes.c_void_p]
        self.api.CredFree.restype = None
        self.kernel = ctypes.WinDLL("Kernel32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        self.kernel.ReleaseMutex.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL

    @contextmanager
    def _exclusive(self, target):
        name = "Local\\RepoWatchCredential-" + sha256(target.encode()).hexdigest()
        mutex = self.kernel.CreateMutexW(None, False, name)
        if not mutex:
            raise CredentialError()
        acquired = False
        try:
            acquired = self.kernel.WaitForSingleObject(mutex, 5000) in {0, 0x80}
            if not acquired:
                raise CredentialError()
            yield
        finally:
            if acquired:
                self.kernel.ReleaseMutex(mutex)
            self.kernel.CloseHandle(mutex)

    def _target(self, reference):
        validate_reference(reference)
        return self.prefix + reference

    def create(self, reference, secret: SecretValue):
        target = self._target(reference)
        self.redactor.register(secret)
        with self._exclusive(target):
            self._create(target, secret)

    def _create(self, target, secret):
        existing = ctypes.POINTER(_Credential)()
        if self.api.CredReadW(target, 1, 0, ctypes.byref(existing)):
            self.api.CredFree(existing)
            raise CredentialError()
        if ctypes.get_last_error() != 1168:  # ERROR_NOT_FOUND
            raise CredentialError()
        encoded = secret.reveal().encode("utf-8")
        blob = (ctypes.c_ubyte * len(encoded)).from_buffer_copy(encoded)
        credential = _Credential()
        credential.Type, credential.Persist = 1, 2
        credential.TargetName, credential.UserName = target, "RepoWatch"
        credential.CredentialBlobSize, credential.CredentialBlob = len(encoded), blob
        try:
            if not self.api.CredWriteW(ctypes.byref(credential), 0):
                raise CredentialError()
        finally:
            ctypes.memset(blob, 0, len(encoded))

    def read(self, reference):
        pointer = ctypes.POINTER(_Credential)()
        if not self.api.CredReadW(self._target(reference), 1, 0, ctypes.byref(pointer)):
            raise CredentialError()
        try:
            credential = pointer.contents
            if not 0 < credential.CredentialBlobSize <= 2000:
                raise CredentialError()
            secret = SecretValue(
                ctypes.string_at(
                    credential.CredentialBlob,
                    credential.CredentialBlobSize,
                ).decode("utf-8")
            )
            self.redactor.register(secret)
            return secret
        except (UnicodeError, ValueError):
            raise CredentialError() from None
        finally:
            self.api.CredFree(pointer)

    def delete(self, reference):
        if (
            not self.api.CredDeleteW(self._target(reference), 1, 0)
            and ctypes.get_last_error() != 1168
        ):
            raise CredentialError()
