# blender-spacecontroller-3d-mouse
# Unofficial Blender add-on for SpaceController 3D mice.
# Copyright (c) 2025 Mikhail Krigman
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

bl_info = {
    "name": "SpaceController 3D Mouse Addon",
    "description": "Use a SpaceControl 3D mouse to navigate the 3D view in Blender.",
    "author": "Mikhail Krigman",
    "version": (1, 1, 0),
    "blender": (4, 4, 0),
    "location": "3D View > N-panel > SpaceController",
    "category": "3D View",
}

import bpy
from bpy.types import Operator, Panel, AddonPreferences
from bpy.props import FloatProperty, BoolProperty
from mathutils import Vector, Euler

from .spacecontroller_device import SpaceControllerDevice, SpaceControllerState

# ---------------------------------------------------------------------------
# Global state: background device + timer
# ---------------------------------------------------------------------------

_device: SpaceControllerDevice | None = None
_enabled: bool = True           # whether we actively use the device
_addon_alive: bool = True       # set False on unregister to stop the timer


# ---------------------------------------------------------------------------
# Addon preferences (tuning sensitivity)
# ---------------------------------------------------------------------------

class SpaceControllerPreferences(AddonPreferences):
    """Global settings for the SpaceController addon."""
    bl_idname = __name__

    move_sensitivity: FloatProperty(
        name="Move Sensitivity",
        default=0.001,
        min=0.00001,
        max=0.1,
        description="Scale factor for translation (tx, ty, tz)",
    )   # type: ignore[valid-type]

    rotate_sensitivity: FloatProperty(
        name="Rotate Sensitivity",
        default=0.0005,
        min=0.00001,
        max=0.1,
        description="Scale factor for rotation (rx, ry, rz)",
    )   # type: ignore[valid-type]

    invert_x: BoolProperty(
        name="Invert X",
        default=False,
        description="Invert X movement",
    )   # type: ignore[valid-type]

    invert_y: BoolProperty(
        name="Invert Y",
        default=False,
        description="Invert Y movement",
    )   # type: ignore[valid-type]

    invert_z: BoolProperty(
        name="Invert Z",
        default=False,
        description="Invert Z movement",
    )   # type: ignore[valid-type]

    enable_rotation: BoolProperty(
        name="Enable Rotation",
        default=True,
        description="Apply controller rotation to the 3D view",
    )   # type: ignore[valid-type]

    def draw(self, _context):
        layout = self.layout
        layout.label(text="SpaceController Settings")
        col = layout.column(align=True)
        col.prop(self, "move_sensitivity")
        col.prop(self, "rotate_sensitivity")
        col.prop(self, "enable_rotation")
        row = col.row(align=True)
        row.label(text="Invert axes:")
        row.prop(self, "invert_x", text="X")
        row.prop(self, "invert_y", text="Y")
        row.prop(self, "invert_z", text="Z")


def get_prefs() -> SpaceControllerPreferences:
    return bpy.context.preferences.addons[__name__].preferences


# ---------------------------------------------------------------------------
# Core view update logic (no modal operator, just functions)
# ---------------------------------------------------------------------------

def _find_first_view3d():
    """Return (area, region, space) for the first VIEW_3D window, or (None, None, None)."""
    wm = bpy.context.window_manager
    if wm is None:
        return None, None, None

    for window in wm.windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                space = area.spaces.active
                for region in area.regions:
                    if region.type == 'WINDOW':
                        return area, region, space
    return None, None, None


def _apply_state_to_area(area, state: SpaceControllerState) -> None:
    """Apply SpaceControllerState to a given VIEW_3D area.

    - Translation: in view (camera) space: right / up / forward.
    - Rotation: orbit around the view pivot (RegionView3D.view_location).
    """
    if area is None or area.type != 'VIEW_3D':
        return

    prefs = get_prefs()
    space = area.spaces.active
    region3d = space.region_3d
    if region3d is None:
        return

    # ----------------------------------------------------------------------
    # TRANSLATION IN VIEW SPACE
    # ----------------------------------------------------------------------
    sx = -1.0 if prefs.invert_x else 1.0
    sy = -1.0 if prefs.invert_y else 1.0
    sz = -1.0 if prefs.invert_z else 1.0

    move_scale = prefs.move_sensitivity

    # Controller translation:
    #   tx: move right / left in view
    #   ty: move up   / down in view
    #   tz: move forward / backward in view
    t_right   = state.tx * move_scale * sx
    t_up      = state.ty * move_scale * sy
    t_forward = state.tz * move_scale * sz

    # Vector in *view* space (camera local)
    v_cam = Vector((t_right, t_up, t_forward))

    # Convert from view space to world space using the view rotation.
    v_world = region3d.view_rotation @ v_cam

    # Move the view pivot in world space = pan/dolly in camera space.
    region3d.view_location += v_world

    # ----------------------------------------------------------------------
    # ROTATION ABOUT PIVOT (RegionView3D.view_location)
    # ----------------------------------------------------------------------
    if prefs.enable_rotation:
        rot_scale = prefs.rotate_sensitivity

        # Map controller rotations to view rotations:
        #   rx: pitch (look up/down)
        #   ry: yaw   (turn left/right)
        #   rz: roll  (tilt head)
        pitch = state.rx * rot_scale
        yaw   = state.ry * rot_scale
        roll  = state.rz * rot_scale

        delta_rot = Euler((pitch, yaw, roll), 'XYZ').to_quaternion()

        # Apply rotation in view-local space:
        region3d.view_rotation = region3d.view_rotation @ delta_rot

    area.tag_redraw()


# ---------------------------------------------------------------------------
# Background timer: behaves like a "device driver" poller
# ---------------------------------------------------------------------------

def _spacecontroller_timer():
    """Timer callback that polls the device and updates the view.

    This runs in the main thread but is *not* a modal operator,
    so it doesn't capture Blender input or block other tools.
    """
    global _device, _enabled, _addon_alive

    # If addon is being unregistered, shut down the timer.
    if not _addon_alive:
        if _device is not None:
            try:
                _device.close()
            except Exception:
                pass
            _device = None
        return None  # stop timer

    # If user disabled the controller, just sleep.
    if not _enabled:
        return 0.5  # check again later

    # Ensure we have a 3D view to control.
    area, region, space = _find_first_view3d()
    if area is None:
        # No 3D view visible yet: try again later.
        return 0.5

    # Open device if needed.
    if _device is None:
        try:
            _device = SpaceControllerDevice(app_name="Blender")
            print("SpaceController: device opened.")
        except Exception as exc:
            print(f"SpaceController: failed to open device: {exc}")
            # Disable so we don't spam errors.
            _enabled = False
            return None

    # Poll device. IMPORTANT: this must be *non-blocking* or have a tiny timeout.
    try:
        state = _device.read_state()
    except Exception as exc:
        print(f"SpaceController: error reading device: {exc}")
        # On error, close device and stop.
        try:
            _device.close()
        except Exception:
            pass
        _device = None
        _enabled = False
        return None

    if state is not None:
        _apply_state_to_area(area, state)

    # Schedule next poll: 0.01s ~ 100 Hz, adjust if needed.
    return 0.01


# ---------------------------------------------------------------------------
# UI: toggle operator + panel
# ---------------------------------------------------------------------------

class SPACECONTROLLER_OT_toggle(Operator):
    """Enable or disable SpaceController background navigation."""
    bl_idname = "spacecontroller.toggle"
    bl_label = "Toggle SpaceController"

    def execute(self, _context):
        global _enabled
        _enabled = not _enabled
        self.report(
            {'INFO'},
            f"SpaceController {'enabled' if _enabled else 'disabled'}."
        )
        return {'FINISHED'}


class VIEW3D_PT_spacecontroller_panel(Panel):
    """Panel in the 3D View's N-panel."""
    bl_label = "SpaceController"
    bl_category = "SpaceController"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'

    def draw(self, _context):
        layout = self.layout
        col = layout.column(align=True)

        # Show status
        status = "Enabled" if _enabled else "Disabled"
        icon = 'CHECKMARK' if _enabled else 'CANCEL'
        row = col.row(align=True)
        row.label(text=f"Status: {status}", icon=icon)

        # Toggle button
        col.operator(
            SPACECONTROLLER_OT_toggle.bl_idname,
            text="Disable" if _enabled else "Enable",
            icon='PAUSE' if _enabled else 'PLAY'
        )

        col.separator()
        col.label(text="Settings in Preferences > Add-ons")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = (
    SpaceControllerPreferences,
    SPACECONTROLLER_OT_toggle,
    VIEW3D_PT_spacecontroller_panel,
)


def register():
    global _addon_alive, _enabled, _device
    _addon_alive = True
    _enabled = True
    _device = None

    for cls in classes:
        bpy.utils.register_class(cls)

    # Start background timer once.
    bpy.app.timers.register(_spacecontroller_timer, first_interval=1.0)


def unregister():
    global _addon_alive, _device
    _addon_alive = False

    # Timer will see _addon_alive == False and clean up device
    if _device is not None:
        try:
            _device.close()
        except Exception:
            pass
        _device = None

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
