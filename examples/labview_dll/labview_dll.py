"""固定 OpenLab LabVIEW DLL ABI 的最小 ``ctypes`` 调用层。"""

from __future__ import annotations

from collections.abc import Mapping
import ctypes
import json
import os
from pathlib import Path
from typing import Any


ABI_VERSION = 1
MAX_RESPONSE_BYTES = 1024 * 1024
_EXPORT_NAMES = ("OLC_Describe", "OLC_Open", "OLC_Invoke", "OLC_Close")
_BYTE_POINTER = ctypes.POINTER(ctypes.c_uint8)
_FUNCTION_ARGUMENTS = (
    _BYTE_POINTER,
    ctypes.c_int32,
    _BYTE_POINTER,
    ctypes.c_int32,
    ctypes.POINTER(ctypes.c_int32),
)


class LabVIEWDLLFailure(RuntimeError):
    """DLL 加载、ABI 或仪表调用失败，并保留框架需要的结构化字段。"""

    def __init__(
        self,
        message: str,
        code: str,
        context: str = "",
        severity: str = "error",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.context = context
        self.severity = severity


class LabVIEWDLL:
    """加载一次 DLL，并用相同二进制签名调用四个导出函数。"""

    def __init__(self, path: Path, *, expected_kind: str) -> None:
        self.path = path.resolve()
        if not self.path.is_file():
            raise LabVIEWDLLFailure(
                f"LabVIEW DLL does not exist: {self.path}",
                "LABVIEW_DLL_NOT_FOUND",
                str(self.path),
            )
        self._dll_directory_cookie = (
            os.add_dll_directory(str(self.path.parent))
            if hasattr(os, "add_dll_directory")
            else None
        )
        try:
            self._library = ctypes.CDLL(str(self.path))
        except OSError as exc:
            raise LabVIEWDLLFailure(
                f"Cannot load LabVIEW DLL: {exc}",
                "LABVIEW_DLL_LOAD_FAILED",
                str(self.path),
            ) from exc

        self._functions: dict[str, Any] = {}
        for name in _EXPORT_NAMES:
            try:
                function = getattr(self._library, name)
            except AttributeError as exc:
                raise LabVIEWDLLFailure(
                    f"LabVIEW DLL does not export {name}",
                    "LABVIEW_DLL_EXPORT_MISSING",
                    name,
                ) from exc
            function.argtypes = list(_FUNCTION_ARGUMENTS)
            function.restype = ctypes.c_int32
            self._functions[name] = function

        self.description = self._call("OLC_Describe", {})
        if self.description.get("abi_version") != ABI_VERSION:
            raise LabVIEWDLLFailure(
                f"LabVIEW DLL ABI must be {ABI_VERSION}",
                "LABVIEW_DLL_ABI_MISMATCH",
                str(self.description.get("abi_version", "")),
            )
        if self.description.get("kind") != expected_kind:
            raise LabVIEWDLLFailure(
                f"LabVIEW DLL kind must be {expected_kind}",
                "LABVIEW_DLL_KIND_MISMATCH",
                str(self.description.get("kind", "")),
            )

    def open(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._call("OLC_Open", payload)

    def invoke(self, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._call(
            "OLC_Invoke",
            {"operation": operation, "parameters": dict(payload)},
        )

    def close(self) -> dict[str, Any]:
        return self._call("OLC_Close", {})

    def _call(
        self,
        export_name: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        try:
            request = json.dumps(
                dict(payload),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise LabVIEWDLLFailure(
                f"Cannot encode {export_name} input: {exc}",
                "LABVIEW_DLL_INPUT_INVALID",
                export_name,
            ) from exc

        request_buffer = (ctypes.c_uint8 * len(request)).from_buffer_copy(request)
        response_buffer = (ctypes.c_uint8 * MAX_RESPONSE_BYTES)()
        response_length = ctypes.c_int32()
        return_code = int(
            self._functions[export_name](
                request_buffer,
                len(request),
                response_buffer,
                MAX_RESPONSE_BYTES,
                ctypes.byref(response_length),
            )
        )
        if return_code != 0:
            raise LabVIEWDLLFailure(
                f"{export_name} returned ABI failure {return_code}",
                "LABVIEW_DLL_CALL_FAILED",
                export_name,
            )
        if not 0 <= response_length.value <= MAX_RESPONSE_BYTES:
            raise LabVIEWDLLFailure(
                f"{export_name} returned invalid response length {response_length.value}",
                "LABVIEW_DLL_RESPONSE_LENGTH_INVALID",
                export_name,
            )

        try:
            decoded = bytes(response_buffer[: response_length.value]).decode("utf-8")
            response = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LabVIEWDLLFailure(
                f"{export_name} returned invalid UTF-8 JSON: {exc}",
                "LABVIEW_DLL_RESPONSE_INVALID",
                export_name,
            ) from exc
        required = {"severity", "code", "message", "context", "result"}
        if not isinstance(response, dict) or set(response) != required:
            raise LabVIEWDLLFailure(
                f"{export_name} returned an invalid response envelope",
                "LABVIEW_DLL_RESPONSE_INVALID",
                export_name,
            )
        if not all(
            isinstance(response[key], str)
            for key in ("severity", "code", "message", "context")
        ) or not isinstance(response["result"], Mapping):
            raise LabVIEWDLLFailure(
                f"{export_name} returned invalid response field types",
                "LABVIEW_DLL_RESPONSE_INVALID",
                export_name,
            )

        severity = response["severity"]
        if severity == "ok":
            return dict(response["result"])
        if severity not in {"warning", "error"}:
            raise LabVIEWDLLFailure(
                f"{export_name} returned unknown severity {severity!r}",
                "LABVIEW_DLL_RESPONSE_INVALID",
                export_name,
            )
        raise LabVIEWDLLFailure(
            response["message"] or f"{export_name} reported {severity}",
            response["code"] or "LABVIEW_DLL_OPERATION_FAILED",
            response["context"],
            severity,
        )


__all__ = ["ABI_VERSION", "LabVIEWDLL", "LabVIEWDLLFailure"]
