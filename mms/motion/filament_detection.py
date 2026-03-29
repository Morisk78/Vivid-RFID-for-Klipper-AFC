# Support for MMS Filament Detection,
# which original module named "Filament Fracture"
#
# Copyright (C) 2024-2026 Garvey Ding <garveyding@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from contextlib import contextmanager
from dataclasses import dataclass

from ..adapters import (
    extruder_adapter,
    gcode_adapter,
    printer_adapter
)
from ..core.exceptions import FilamentDetectionFailedError
from ..core.slot_pin import PinType


@dataclass(frozen=True)
class MMSFilamentDetectionConfig:
    log_flag: str = "==X=="
    lower_temp: float = 0


class MMSFilamentDetection:
    def __init__(self):
        fd_config = MMSFilamentDetectionConfig()
        self.log_flag = fd_config.log_flag
        self.lower_temp = fd_config.lower_temp

        self.pin_type = PinType()

        # Status
        self._enable = False
        self._enable_config = False
        self._activating = False
        self._activating_slot_num = None

        # Klippy event handler
        printer_adapter.register_klippy_ready(
            self._handle_klippy_ready)

    def _handle_klippy_ready(self):
        self._initialize_mms()
        # self._initialize_gcode()
        self._initialize_loggers()
        self._register()

    def _initialize_mms(self):
        self.mms = printer_adapter.get_mms()
        self.mms_delivery = printer_adapter.get_mms_delivery()

        self.mms_pause = self.mms.get_mms_pause()
        self._enable = self.mms.filament_detection_is_enabled()
        self._enable_config = self._enable

    def _initialize_loggers(self):
        mms_logger = printer_adapter.get_mms_logger()
        self.log_info = mms_logger.create_log_info(console_output=True)
        # self.log_warning = mms_logger.create_log_warning(console_output=True)

        self.log_info_s = mms_logger.create_log_info(console_output=False)
        self.log_warning_s = mms_logger.create_log_warning(
            console_output=False)
        self.log_error_s = mms_logger.create_log_error(console_output=False)

    # ---- Status ----
    def is_enabled(self):
        return self._enable

    def is_activating(self):
        return self._activating

    # ---- Register ----
    def _register(self):
        if not self._enable:
            return
        for mms_slot in self.mms.get_mms_slots():
            slot_num = mms_slot.get_num()
            mms_slot.inlet.add_release_callback(
                callback=self._handle_inlet_is_released,
                params={"slot_num":slot_num}
            )
            mms_slot.gate.add_release_callback(
                callback=self._handle_gate_is_released,
                params={"slot_num":slot_num}
            )

    def _unregister(self):
        # if not self._enable:
        #     return
        for mms_slot in self.mms.get_mms_slots():
            mms_slot.inlet.remove_release_callback(
                callback=self._handle_inlet_is_released
            )
            mms_slot.gate.remove_release_callback(
                callback=self._handle_gate_is_released
            )

    # ---- Handlers ----
    def _common_handler(self, slot_num, pin_type):
        if not self._enable \
            or not self._activating \
            or slot_num != self._activating_slot_num:
            return

        msg = f"slot[{slot_num}] filament detection: " + \
            f"{pin_type} is released {self.log_flag}"

        try:
            # Log with respond_error() would also print console log,
            # so just warning slient
            self.log_warning_s(msg)
            gcode_adapter.respond_error(msg)

            # Stop delivery
            self.mms_delivery.mms_stop(slot_num)
            # Free selecting slot
            self.mms_delivery.mms_unselect()

            if self.mms.printer_is_printing():
                # Pause print
                if not self.mms_pause.mms_pause():
                    raise FilamentDetectionFailedError(
                        f"slot[{slot_num}] pause print failed",
                        self.mms.get_mms_slot(slot_num)
                    )
                # Cool down extruder, no wait
                extruder_adapter.set_temperature(
                    temp=self.lower_temp, wait=False
                )
                # Blocking wait toolhead to complete pause movement
                self.mms_delivery.wait_toolhead()

        except FilamentDetectionFailedError as e:
            self.log_error_s(e)
        except Exception as e:
            self.log_error_s(f"{msg} error: {e}")

    def _handle_inlet_is_released(self, slot_num):
        self._common_handler(slot_num, self.pin_type.inlet)

    def _handle_gate_is_released(self, slot_num):
        self._common_handler(slot_num, self.pin_type.gate)

    def force_handle_inlet_is_released(self, slot_num):
        if not self._enable:
            return

        org_slot_num = self._activating_slot_num
        org_status = self._activating

        self._activating = True
        self._activating_slot_num = slot_num

        self._handle_inlet_is_released(slot_num)

        self._activating_slot_num = org_slot_num
        self._activating = org_status

    # ---- Control ----
    def enable(self):
        self._enable = True
        self.log_info_s("MMS filament detection is enabled")

    def disable(self):
        self._enable = False
        self.deactivate()
        self.log_info_s("MMS filament detection is disabled")

    def recover(self):
        if self._enable is not self._enable_config:
            if self._enable_config:
                self.enable()
            else:
                self.disable()

    def activate(self, slot_num):
        if not self._enable:
            return
        self._activating = True
        self._activating_slot_num = slot_num
        self.log_info_s(
            f"slot[{slot_num}] filament detection is activated")

    def deactivate(self):
        if not self._enable:
            return
        msg_num = self._activating_slot_num \
            if self._activating_slot_num is not None else '*'
        self._activating = False
        self._activating_slot_num = None
        self.log_info_s(
            f"slot[{msg_num}] filament detection is deactivated")

    @contextmanager
    def monitor(self, slot_num):
        self.activate(slot_num)
        try:
            yield
        finally:
            self.deactivate()
