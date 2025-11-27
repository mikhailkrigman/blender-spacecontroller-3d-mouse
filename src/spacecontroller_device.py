# blender-spacecontroller-3d-mouse
# Unofficial Blender add-on for SpaceController 3D mice.
# Copyright (c) 2025 Mikhail Krigman
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.


"""
Minimal Python wrapper for the SpaceController DLL.

This file is completely independent from the original Blender plugin.
It only uses the vendor DLL (spc_ctrlr_32/64.dll) via ctypes.
"""

from dataclasses import dataclass
from typing import Optional

import ctypes
import sys
import platform


@dataclass
class SpaceControllerState:
    """Single snapshot of controller state."""
    tx: float  # translation X
    ty: float  # translation Y
    tz: float  # translation Z
    rx: float  # rotation X
    ry: float  # rotation Y
    rz: float  # rotation Z
    event: int  # event / buttons (raw int from DLL)


class SpaceControllerDevice:
    """
    Minimal interface to a SpaceController device via the SpaceControl DLL.

    Workflow:
    - __init__(): load DLL, connect, pick first device
    - read_state(): poll current state (or return None if nothing)
    - close(): disconnect cleanly
    """

    def __init__(self, app_name: str = "Blender"):
        self._lib = self._load_library()
        self._setup_function_signatures()
        self._device_id = self._connect_and_get_first_device(app_name)

    # ------------------------------------------------------------------
    # DLL loading and function signatures
    # ------------------------------------------------------------------
    def _load_library(self) -> ctypes.CDLL:
        """Load the SpaceControl controller DLL depending on platform."""
        if sys.platform != "win32":
            raise RuntimeError("This simple wrapper currently only supports Windows.")

        arch = platform.architecture()[0]

        if arch == "32bit":
            dll_path = "spc_ctrlr_32.dll"
        else:
            # This is the same path that the original plugin used.
            dll_path = r"C:\Program Files (x86)\SpaceControl\libs\win64\spc_ctrlr_64.dll"

        try:
            lib = ctypes.CDLL(dll_path)
        except OSError as exc:
            raise RuntimeError(f"Could not load SpaceController DLL at '{dll_path}': {exc}") from exc

        return lib

    def _setup_function_signatures(self) -> None:
        """Declare argument and return types for the DLL functions we use."""
        # int scConnect2(bool useDaemon, const char* applicationName);
        self._lib.scConnect2.argtypes = [ctypes.c_bool, ctypes.c_char_p]
        self._lib.scConnect2.restype = ctypes.c_int

        # int scDisconnect();
        self._lib.scDisconnect.argtypes = []
        self._lib.scDisconnect.restype = ctypes.c_int

        # int scGetDevNum(int* numAll, int* numUsb, int* numOther);
        self._lib.scGetDevNum.argtypes = [
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]
        self._lib.scGetDevNum.restype = ctypes.c_int

        # int scFetchStdData(
        #   int devId,
        #   short* x, short* y, short* z,
        #   short* a, short* b, short* c,
        #   int* wheel, int* buttons, int* event,
        #   long* tvSec, long* tvUsec);
        self._lib.scFetchStdData.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_short),  # x
            ctypes.POINTER(ctypes.c_short),  # y
            ctypes.POINTER(ctypes.c_short),  # z
            ctypes.POINTER(ctypes.c_short),  # a
            ctypes.POINTER(ctypes.c_short),  # b
            ctypes.POINTER(ctypes.c_short),  # c
            ctypes.POINTER(ctypes.c_int),    # wheel
            ctypes.POINTER(ctypes.c_int),    # buttons
            ctypes.POINTER(ctypes.c_int),    # event
            ctypes.POINTER(ctypes.c_long),   # tvSec
            ctypes.POINTER(ctypes.c_long),   # tvUsec
        ]
        self._lib.scFetchStdData.restype = ctypes.c_int

    # ------------------------------------------------------------------
    # Connection / device discovery
    # ------------------------------------------------------------------
    def _connect_and_get_first_device(self, app_name: str) -> int:
        """Connect to the SpaceControl daemon/driver and pick the first device."""
        result = self._lib.scConnect2(
            ctypes.c_bool(False),                      # don't use daemon (same as original plugin)
            ctypes.c_char_p(app_name.encode("ascii")), # identify as "Blender"
        )
        if result != 0:
            raise RuntimeError(f"scConnect2 failed with status {result}")

        num_all = ctypes.c_int()
        num_usb = ctypes.c_int()
        num_other = ctypes.c_int()

        status = self._lib.scGetDevNum(
            ctypes.byref(num_all),
            ctypes.byref(num_usb),
            ctypes.byref(num_other),
        )
        if status != 0:
            raise RuntimeError(f"scGetDevNum failed with status {status}")

        if num_all.value <= 0:
            raise RuntimeError("No SpaceController devices found.")

        # The C API uses 0-based device indices. We just take the first one.
        return 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def read_state(self) -> Optional[SpaceControllerState]:
        """
        Poll current state from the device.

        Returns:
            SpaceControllerState if new data was read, or None if there
            was no new data / an error occurred.
        """
        if self._device_id is None:
            return None

        x = ctypes.c_short()
        y = ctypes.c_short()
        z = ctypes.c_short()
        a = ctypes.c_short()
        b = ctypes.c_short()
        c = ctypes.c_short()
        wheel = ctypes.c_int()
        buttons = ctypes.c_int()
        event = ctypes.c_int()
        tv_sec = ctypes.c_long()
        tv_usec = ctypes.c_long()

        status = self._lib.scFetchStdData(
            ctypes.c_int(self._device_id),
            ctypes.byref(x),
            ctypes.byref(y),
            ctypes.byref(z),
            ctypes.byref(a),
            ctypes.byref(b),
            ctypes.byref(c),
            ctypes.byref(wheel),
            ctypes.byref(buttons),
            ctypes.byref(event),
            ctypes.byref(tv_sec),
            ctypes.byref(tv_usec),
        )

        # According to the original code: status == 0 means "OK".
        if status != 0:
            return None

        return SpaceControllerState(
            tx=float(x.value),
            ty=float(y.value),
            tz=float(z.value),
            rx=float(a.value),
            ry=float(b.value),
            rz=float(c.value),
            event=int(event.value),
        )

    def close(self) -> None:
        """Disconnect from the driver."""
        try:
            if hasattr(self, "_lib"):
                self._lib.scDisconnect()
        except Exception:
            # We don't care if disconnect fails on shutdown.
            pass
