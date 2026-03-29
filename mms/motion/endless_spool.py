# Support for MMS Endless Spool
# Well... I prefer to call it "Limit Spool"
#
# Copyright (C) 2026 Garvey Ding <garveyding@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from dataclasses import dataclass

from ..adapters import (
    extruder_adapter,
    gcode_adapter,
    printer_adapter
)
from ..core.exceptions import EndlessSpoolFailedError
from ..core.slot_pin import PinType


@dataclass(frozen=True)
class MMSEndlessSpoolConfig:
    log_flag: str = "==O=="
    truncate_distance: float = 2000
    truncate_orphan_distance: float = 200
    extrude_distance_max: float = 5000
    hint_prompt: str = (
        "The filament in slot[{slot_num}] has runout. "
        "Please clear any remaining filament. "
        "After inserting a new filament, "
        "click RESUME to resume printing."
    )

    def get_hint(self, slot_num):
        return self.hint_prompt.format(slot_num=slot_num)


class MMSEndlessSpool:
    def __init__(self):
        self.es_config = MMSEndlessSpoolConfig()
        self.log_flag = self.es_config.log_flag
        self.truncate_distance = self.es_config.truncate_distance
        self.truncate_orphan_distance = self.es_config.truncate_orphan_distance
        self.extrude_distance_max = self.es_config.extrude_distance_max

        self.pin_type = PinType()

        # Status
        self._enable = False
        self._activating = False

        self._inlet_slot_num = None

        # Klippy event handler
        printer_adapter.register_klippy_ready(
            self._handle_klippy_ready)

    def _handle_klippy_ready(self):
        self._initialize_mms()
        self._initialize_loggers()
        self._register()

    def _initialize_mms(self):
        self.mms = printer_adapter.get_mms()
        self.mms_brush = printer_adapter.get_mms_brush()
        self.mms_charge = printer_adapter.get_mms_charge()
        self.mms_delivery = printer_adapter.get_mms_delivery()
        self.mms_purge = printer_adapter.get_mms_purge()
        self.mms_swap = printer_adapter.get_mms_swap()

        self.mms_pause = self.mms.get_mms_pause()
        self.mms_resume = self.mms.get_mms_resume()
        self.mms_fil_detection = self.mms.get_mms_filament_detection()

        self._enable = self.mms.endless_spool_is_enabled() \
            and self.mms_purge.is_enabled()

    def _initialize_loggers(self):
        mms_logger = printer_adapter.get_mms_logger()
        self.log_info = mms_logger.create_log_info(console_output=True)
        self.log_warning = mms_logger.create_log_warning(console_output=True)

        self.log_info_s = mms_logger.create_log_info(console_output=False)
        self.log_warning_s = mms_logger.create_log_warning(
            console_output=False)
        self.log_error_s = mms_logger.create_log_error(console_output=False)

    # ---- Register ----
    def _register(self):
        for mms_slot in self.mms.get_mms_slots():
            slot_num = mms_slot.get_num()

            mms_slot.inlet.add_release_callback(
                callback=self._handle_inlet_is_released,
                params={"slot_num":slot_num}
            )

            if self.mms.get_entry():
                # Entry is set
                mms_slot.entry.add_release_callback(
                    callback=self._handle_entry_is_released,
                    params={"slot_num":slot_num}
                )
            else:
                mms_slot.gate.add_release_callback(
                    callback=self._handle_gate_is_released,
                    params={"slot_num":slot_num}
                )

        # Register callbacks
        print_observer = self.mms.get_print_observer()
        print_observer.register_pause_callback(self.deactivate)
        print_observer.register_finish_callback(self.deactivate)
        print_observer.register_resume_callback(self.activate)

    def _unregister(self):
        for mms_slot in self.mms.get_mms_slots():
            slot_num = mms_slot.get_num()

            mms_slot.inlet.remove_release_callback(
                callback=self._handle_inlet_is_released)

            # Entry is set
            if self.mms.get_entry():
                mms_slot.entry.remove_release_callback(
                    callback=self._handle_entry_is_released)
            else:
                mms_slot.gate.remove_release_callback(
                    callback=self._handle_gate_is_released)

        print_observer = self.mms.get_print_observer()
        print_observer.unregister_pause_callback(self.deactivate)
        print_observer.unregister_finish_callback(self.deactivate)
        print_observer.unregister_resume_callback(self.activate)

    # ---- Status ----
    # def is_enabled(self):
    #     return self._enable

    def is_activating(self):
        return self._activating

    def is_inlet_released(self, slot_num):
        return self._activating \
            and self._inlet_slot_num is not None \
            and slot_num == self._inlet_slot_num

    # ---- Handlers ----
    def _can_handle(self, slot_num):
        slot_num_c = self.mms_charge.get_charged_slot()

        if self._activating \
            and self.mms.printer_is_printing() \
            and slot_num_c is not None \
            and slot_num == slot_num_c:
            return True

        # Not activating/not printing/self is not charged slot
        return False

    def _pause(self, slot_num):
        mms_slot = self.mms.get_mms_slot(slot_num)
        mms_buffer = mms_slot.get_mms_buffer()
        mms_buffer.deactivate_monitor()

        # Pause print
        if not self.mms_pause.mms_pause():
            # Pause failed
            raise EndlessSpoolFailedError(
                f"slot[{slot_num}] endless spool pause failed",
                mms_slot
            )

        # Setup resume after pause is success
        self.mms_resume.set_mms_swap_resume(
            func=self.mms_swap.cmd_SWAP,
            gcmd=gcode_adapter.easy_gcmd(
                command=self.mms_swap.format_command(slot_num)
            )
        )

    def _pause_and_hint(self, slot_num):
        self._pause(slot_num)
        # No exception raise, activate led effect
        self.mms.get_mms_slot().slot_led.activate_blinking()
        self.log_info(self.es_config.get_hint(slot_num))

    def _pause_and_wait(self, slot_num):
        self._pause(slot_num)

        # Wait Toolhead idle
        if not self.mms_delivery.wait_toolhead():
            raise EndlessSpoolFailedError(
                f"slot[{slot_num}] endless spool wait toolhead idle timeout",
                self.mms.get_mms_slot(slot_num)
            )

    def _purge_long_distance(self, distance):
        speed = self.mms_purge.get_purge_speed()
        distance_once = self.mms_purge.get_purge_distance()
        distance_extruded = 0

        # Extrude until distance is satisfied
        while distance_extruded < distance:
            # Move to tray
            self.mms_purge.move_to_tray()
            # Extrude
            dist = min(distance_once, distance-distance_extruded)
            extruder_adapter.extrude(dist, speed)
            # Brush to clean nozzle
            if self.mms_brush.is_enabled():
                self.mms_brush.mms_brush()
            # Sum distance
            distance_extruded += dist

    def _find_slot_new(self, slot_num):
        slot_num_new = None
        slot_num_checked = [slot_num]
        mms_slot = self.mms.get_mms_slot(slot_num)

        while not slot_num_new:
            slot_num_sub = mms_slot.get_endless_with_slot()
            # No sub, sub is slot itself, sub has been checked
            if slot_num_sub is None or slot_num_sub in slot_num_checked:
                break

            slot_num_checked.append(slot_num_sub)

            mms_slot_sub = self.mms.get_mms_slot(slot_num_sub)
            if mms_slot_sub.inlet.is_triggered():
                slot_num_new = slot_num_sub
            else:
                slot_num = slot_num_sub

        return slot_num_new

    def _update_mapping_and_resume(self, slot_num):
        slot_num_new = self._find_slot_new(slot_num)
        if slot_num_new is None:
            raise EndlessSpoolFailedError(
                f"slot[{slot_num}] have no mapping slot",
                self.mms.get_mms_slot(slot_num)
            )
            return
        self.mms_swap.update_mapping_slot_num(slot_num, slot_num_new)
        self.mms_resume.gcode_resume()

    # ---- Handlers ----
    def _log_msg(self, slot_num, p_type):
        msg = f"slot[{slot_num}] '{p_type}' released, endless spool handling"
        self.log_info(f"{msg} {self.log_flag}")

    def _handle_inlet_is_released(self, slot_num):
        if not self._can_handle(slot_num):
            return
        self._log_msg(slot_num, self.pin_type.inlet)
        self._inlet_slot_num = slot_num

    def _handle_gate_is_released(self, slot_num):
        if not self._can_handle(slot_num):
            return

        if not self._enable:
            self._pause_and_hint(slot_num)
            return

        p_type = self.pin_type.gate
        self._log_msg(slot_num, p_type)

        try:
            self._pause_and_wait(slot_num)

            # If Gate is released but
            # Inlet is still triggered, just resume
            mms_slot = self.mms.get_mms_slot(slot_num)
            if mms_slot.inlet.is_triggered():
                self.log_info(
                    f"slot[{slot_num}] '{self.pin_type.inlet}' "
                    "is triggered, resume immediately"
                )
                self.mms_resume.gcode_resume()
                return

            # Make sure is not selecting
            self.mms_delivery.mms_unselect()
            # Purge long distance to truncate bowden
            self.log_info(
                f"slot[{slot_num}] purge "
                f"{self.truncate_distance} mm begin"
            )
            self._purge_long_distance(self.truncate_distance)

            # Finally update slot mapping and resume
            self._update_mapping_and_resume(slot_num)

        except EndlessSpoolFailedError as e:
            self.log_error_s(e)
        except Exception as e:
            self.log_error_s(
                f"slot[{slot_num}] '{p_type}' released, "
                f"endless spool handle error: {e}"
            )

    def _handle_entry_is_released(self, slot_num):
        if not self._can_handle(slot_num):
            return

        if not self._enable:
            self._pause_and_hint(slot_num)
            return

        p_type = self.pin_type.entry
        self._log_msg(slot_num, p_type)

        try:
            self._pause_and_wait(slot_num)

            # If Entry is released but
            # Inlet/Gate is still triggered, just resume
            mms_slot = self.mms.get_mms_slot(slot_num)
            if mms_slot.inlet.is_triggered():
                self.log_info(
                    f"slot[{slot_num}] '{self.pin_type.inlet}' "
                    "is triggered, resume immediately"
                )
                self.mms_resume.gcode_resume()
                return
            if mms_slot.gate.is_triggered():
                self.log_info(
                    f"slot[{slot_num}] '{self.pin_type.gate}' "
                    "is triggered, resume immediately"
                )
                self.mms_resume.gcode_resume()
                return

            # Finally update slot mapping and resume
            self._update_mapping_and_resume(slot_num)

        except EndlessSpoolFailedError as e:
            self.log_error_s(e)
        except Exception as e:
            self.log_error_s(
                f"slot[{slot_num}] '{p_type}' released, "
                f"endless spool handle error: {e}"
            )

    # ---- Control ----
    def activate(self):
        self._activating = True
        self.log_info_s("MMS endless spool is activated")
        self.mms_fil_detection.disable()

    def deactivate(self):
        self._activating = False
        self.log_info_s("MMS endless spool is deactivated")
        self.mms_fil_detection.recover()
        self._inlet_slot_num = None

    def purge_truncate(self, slot_num):
        # Self is not charged slot, skip
        slot_num_c = self.mms_charge.get_charged_slot()
        if slot_num_c is None or slot_num != slot_num_c:
            return

        mms_slot = self.mms.get_mms_slot(slot_num)
        if mms_slot.inlet.is_triggered():
            return

        try:
            slot_num_new = self._find_slot_new(slot_num)
            if slot_num_new is None:
                raise EndlessSpoolFailedError(
                    f"slot[{slot_num}] have no mapping slot",
                    self.mms.get_mms_slot(slot_num)
                )
                return
            self.mms_swap.update_mapping_slot_num(slot_num, slot_num_new)
        except EndlessSpoolFailedError as e:
            self.log_warning(e)
            return

        self.log_info_s(
            f"slot[{slot_num}] endless spool purge truncate begin")

        if mms_slot.entry.is_set():
            self._purge_until_entry_release(slot_num)
            self._purge_long_distance(self.truncate_orphan_distance)
            return
        else:
            self._purge_long_distance(self.truncate_distance)

        self.log_info_s(
            f"slot[{slot_num}] endless spool purge truncate finish")

    def _purge_until_entry_release(self, slot_num):
        mms_slot = self.mms.get_mms_slot(slot_num)
        success = True

        # Check if entry sensor is set and triggered
        if mms_slot.entry_is_triggered():
            speed = self.mms_purge.get_purge_speed()
            distance = self.mms_purge.get_purge_distance()
            distance_extruded = 0

            # Make sure is not selecting
            self.mms_delivery.mms_unselect()
            # Extrude until entry is released
            while mms_slot.entry_is_triggered():
                # Move to tray
                self.mms_purge.move_to_tray()
                # Extrude
                extruder_adapter.extrude(distance, speed)
                # Brush to clean nozzle
                if self.mms_brush.is_enabled():
                    self.mms_brush.mms_brush()

                # Distance check
                distance_extruded += distance
                if distance_extruded >= self.extrude_distance_max:
                    self.log_warning_s(
                        f"slot[{slot_num}] total extrude distance "
                        f"reach limit {self.extrude_distance_max}mm, "
                        "break"
                    )
                    success = False
                    break

            return success
