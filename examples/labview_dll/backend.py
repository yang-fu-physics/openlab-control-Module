"""把固定 OpenLab LabVIEW DLL ABI 接入 Measurement Module 生命周期。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, NoReturn

from labcontrol.module_api import ModuleAPI, ModuleError, ModuleWarning

from .labview_dll import LabVIEWDLL, LabVIEWDLLFailure


DLL_PATH = Path(__file__).with_name("driver.dll")


class Module:
    """一个模块进程只加载一次 DLL；同一模块内的调用保持串行。"""

    sequence_commands = (
        {
            "id": "set_value",
            "label": "Set Value",
            "description": "Example ordinary command; replace its name, range, and DLL behavior.",
            "kind": "command",
            "fields": [
                {
                    "name": "value",
                    "label": "Value",
                    "type": "float",
                    "default": 0.0,
                    "minimum": -1.0,
                    "maximum": 1.0,
                    "decimals": 3,
                }
            ],
        },
        {
            "id": "scan_value",
            "label": "Scan Value",
            "description": "Example scan command; the core invokes the DLL once per point.",
            "kind": "scan",
            "points_field": "points",
            "point_parameter": "value",
            "fields": [
                {
                    "name": "points",
                    "label": "Points",
                    "type": "list",
                    "default": [0.0, 1.0],
                },
                {
                    "name": "settle_seconds",
                    "label": "Settle Time",
                    "type": "float",
                    "default": 0.0,
                    "minimum": 0.0,
                    "unit": "s",
                    "decimals": 3,
                },
            ],
        },
    )

    def __init__(self) -> None:
        self._driver = LabVIEWDLL(DLL_PATH, expected_kind="measurement_module")
        description = self._driver.description
        columns = description.get("columns")
        display_columns = description.get("display_columns")
        slots = description.get("slots")
        if (
            not isinstance(columns, Mapping)
            or not columns
            or not all(
                isinstance(name, str) and name and isinstance(unit, str)
                for name, unit in columns.items()
            )
        ):
            raise ModuleError(
                "DLL description contains invalid columns",
                "LABVIEW_DLL_DESCRIPTION_INVALID",
                "columns",
            )
        if (
            not isinstance(display_columns, list)
            or not display_columns
            or not all(
                isinstance(name, str) and name in columns
                for name in display_columns
            )
        ):
            raise ModuleError(
                "DLL description contains invalid display_columns",
                "LABVIEW_DLL_DESCRIPTION_INVALID",
                "display_columns",
            )
        if not isinstance(slots, int) or isinstance(slots, bool) or slots < 1:
            raise ModuleError(
                "DLL description contains invalid slots",
                "LABVIEW_DLL_DESCRIPTION_INVALID",
                "slots",
            )
        self.columns = dict(columns)
        self.display_columns = tuple(display_columns)
        self.slots = slots
        self._close_required = False

    @staticmethod
    def _raise_failure(error: LabVIEWDLLFailure) -> NoReturn:
        exception = ModuleWarning if error.severity == "warning" else ModuleError
        raise exception(str(error), error.code, error.context) from error

    def open(self, api: ModuleAPI) -> Mapping[str, Any]:
        api.checkpoint()
        self._close_required = True
        try:
            status = self._driver.open(
                {
                    "resources": dict(api.resources()),
                    "operation_timeout_seconds": api.timeout,
                }
            )
        except LabVIEWDLLFailure as error:
            self._raise_failure(error)
        api.status(status)
        return status

    def configure(
        self,
        settings: Mapping[str, Any],
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        api.checkpoint()
        try:
            status = self._driver.invoke(
                "configure",
                {
                    "settings": dict(settings),
                    "resources": dict(api.resources()),
                    "operation_timeout_seconds": api.timeout,
                },
            )
        except LabVIEWDLLFailure as error:
            self._raise_failure(error)
        api.status(status)
        return status

    def measure(self, slot: int, api: ModuleAPI):
        api.checkpoint()
        try:
            result = self._driver.invoke(
                "measure",
                {"slot": slot, "operation_timeout_seconds": api.timeout},
            )
        except LabVIEWDLLFailure as error:
            self._raise_failure(error)
        api.checkpoint()
        row = result.get("row")
        if not isinstance(row, Mapping):
            raise ModuleError(
                "DLL measure result does not contain a row object",
                "LABVIEW_DLL_RESULT_INVALID",
                "measure.row",
            )
        status = result.get("status")
        if status is not None:
            if not isinstance(status, Mapping):
                raise ModuleError(
                    "DLL measure status is not an object",
                    "LABVIEW_DLL_RESULT_INVALID",
                    "measure.status",
                )
            api.status(status)
        rawdata = result.get("rawdata")
        return dict(row) if rawdata is None else (dict(row), rawdata)

    def on_event(
        self,
        event: str,
        data: Mapping[str, Any],
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        api.checkpoint()
        try:
            status = self._driver.invoke(
                "event",
                {
                    "name": event,
                    "data": dict(data),
                    "operation_timeout_seconds": api.timeout,
                },
            )
        except LabVIEWDLLFailure as error:
            self._raise_failure(error)
        api.status(status)
        return status

    def execute_sequence_command(
        self,
        command_id: str,
        parameters: Mapping[str, Any],
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        api.checkpoint()
        try:
            status = self._driver.invoke(
                "sequence_command",
                {
                    "command_id": command_id,
                    "parameters": dict(parameters),
                    "operation_timeout_seconds": api.timeout,
                },
            )
        except LabVIEWDLLFailure as error:
            self._raise_failure(error)
        api.checkpoint()
        api.status(status)
        return status

    def close(self, api: ModuleAPI) -> Mapping[str, Any]:
        if not self._close_required:
            return {}
        try:
            status = self._driver.close()
        except LabVIEWDLLFailure as error:
            self._raise_failure(error)
        self._close_required = False
        api.status(status)
        return status
