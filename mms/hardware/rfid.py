# Support for MMS RFID Reader: mfrc522
#
# Copyright (C) 2024-2025 Garvey Ding <garveyding@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import json
import os
import logging
from contextlib import contextmanager
from dataclasses import dataclass, fields

from ...bus import MCU_SPI_from_config

from .mfrc522 import (
    HashAssistant,
    MFRC522Handler,
    RFIDCache,
    RFIDModel
)
from ..adapters import gcode_adapter, printer_adapter
from ..core.task import PeriodicTask


class _StandaloneLogger:
    def __init__(self, name="mms_rfid_standalone"):
        self._logger = logging.getLogger(name)

    def create_log_info(self, console_output=True):
        return lambda msg: self._logger.info(msg)

    def create_log_warning(self):
        return lambda msg: self._logger.warning(msg)

    def create_log_error(self):
        return lambda msg: self._logger.error(msg)


@dataclass(frozen=True)
class RFIDEvent:
    tag_detected: str = "rfid:tag:detected"
    tag_data: str = "rfid:tag:data"


@dataclass(frozen=True)
class RFIDConfig:
    printer_config: object

    period: float = 0.1
    timeout: float = 60.0

    skip_configs = [
        "printer_config",
        "period",
        "timeout",
    ]

    cs_pin: str = ""
    spi_bus: str = ""
    slots: str = ""
    rfid_data_file: str = ""

    def __post_init__(self):
        type_method_map = {
            str: "get",
            int: "getint",
            float: "getfloat",
            list: "getintlist",
        }

        for field_info in fields(self):
            field_name = field_info.name
            field_type = field_info.type

            if field_name in self.skip_configs:
                continue

            if field_name == "slots":
                config_value = self._parse_string_list(field_name="slots")
                object.__setattr__(self, field_name, config_value)
                continue

            get_method = type_method_map.get(field_type, "get")
            config_value = getattr(self.printer_config, get_method)(field_name)
            object.__setattr__(self, field_name, config_value)

    def _parse_string_list(self, field_name):
        val_str = self.printer_config.get(field_name) or ""
        return [int(val.strip()) for val in val_str.split(",") if val.strip().isdigit()]


class RFIDManager:
    def __init__(self, spi):
        self.handler = MFRC522Handler(spi)
        self.hash_assistant = HashAssistant()
        self._initialize_loggers()
        self.cache = RFIDCache(max_size=32)

    def _initialize_loggers(self):
        try:
            mms_logger = printer_adapter.get_mms_logger()
        except Exception:
            mms_logger = _StandaloneLogger("mms_rfid_manager")

        self.log_info = mms_logger.create_log_info(console_output=True)
        self.log_warning = mms_logger.create_log_warning()
        self.log_error = mms_logger.create_log_error()
        self.log_info_s = mms_logger.create_log_info(console_output=False)

    def new_rfid_model(self):
        return RFIDModel()

    def to_string(self, block_data):
        return self.handler.format_block_data(block_data)

    @contextmanager
    def use_antenna(self):
        with self.handler.antenna_manager():
            yield

    def get_version(self):
        with self.use_antenna():
            return hex(self.handler.get_version()).upper().zfill(2)

    def get_uid(self):
        with self.use_antenna():
            return self.handler.read_uid()

    def read_with_uid(self, uid):
        with self.use_antenna():
            self.handler.picc_select(uid)
            sector_15_lst = self.handler.read_sector(uid=uid, sector_num=15)
            sector_15_lst.sort(key=lambda tup: tup[0])
            blocks_lst = list(filter(lambda tup: tup[0] in [60, 61], sector_15_lst))
            hash_read = self.hash_assistant.block_to_string(blocks_lst)
            self.log_info_s(f"hash_read: {hash_read}")

    def rfid_read(self):
        with self.use_antenna():
            uid = self.handler.prepare_loop()
            if not uid:
                self.log_info_s("No Tag, return")
                return

            uid_s = self.handler.format_block_data(uid)

            sector_15_lst = self.handler.read_sector(uid=uid, sector_num=15)
            sector_15_lst.sort(key=lambda tup: tup[0])
            blocks_lst = list(filter(lambda tup: tup[0] in [60, 61], sector_15_lst))

            hash_read = self.hash_assistant.block_to_string(blocks_lst)
            self.log_info_s(f"hash_read: {hash_read}")

            if not hash_read:
                self.log_error(f"Hash block read error with UID: {uid_s}")
                return

            if not self.hash_assistant.is_valid_length(hash_read):
                self.log_error(f"The hash data has wrong length: {hash_read}")
                return

            if not self.hash_assistant.is_hexadecimal(hash_read):
                self.log_error(f"The hash data is not hex: {hash_read}")
                return

            if self.hash_assistant.has_high_zero_ratio(hash_read):
                self.log_error(f"The hash data has high zero ratio: {hash_read}")
                return

            cache_key = self.cache.gen_key(uid_s)
            blocks_cached = self.cache.get(cache_key)
            need_reload = False

            if blocks_cached:
                self.log_info_s("cache load")

                blocks_cached.sort(key=lambda tup: tup[0])
                blocks_hash = list(filter(lambda tup: tup[0] in [60, 61], blocks_cached))

                hash_cached = self.hash_assistant.block_to_string(blocks_hash)
                self.log_info_s(f"hash_cached: {hash_cached}")

                if hash_read == hash_cached:
                    self.log_info_s("cached found and hash match, return blocks cached")
                    cache_key = self.cache.gen_key(uid_s, prefix="rfid_dict")
                    return self.cache.get(cache_key)
                else:
                    self.log_info_s("cache not the same, reload")
                    need_reload = True
            else:
                self.log_info_s("init load...")
                need_reload = True

            if need_reload:
                uid_new = self.handler.prepare_loop()
                if not uid_new:
                    self.log_info_s("no Tag, reload failed, exit")
                    return

                uid_new_s = self.handler.format_block_data(uid_new)
                if uid_new_s != uid_s:
                    self.log_info_s(f"UID begin: {uid_s}")
                    self.log_info_s(f"UID current: {uid_new_s}")
                    self.log_info_s("found different UID, reload failed, exit")
                    return

                blocks_read = self.handler.read_all_loop(uid)
                if not blocks_read:
                    self.log_info_s("failed to Read all blocks data while reloading, exit")
                    return

                blocks_read.sort(key=lambda tup: tup[0])
                data_string = self.hash_assistant.block_to_string(blocks_read[:60])
                hash_calculate = self.hash_assistant.hash_as_string(data_string)
                self.log_info_s(f"hash_calculate: {hash_calculate}")

                if hash_read != hash_calculate:
                    self.log_error("read hash block data not equal to calculated, exit")
                    return

                cache_key = self.cache.gen_key(uid_s)
                self.cache.add(cache_key, blocks_read)
                self.log_info_s(f"RFID data success cached with UID: {uid_s}")

                blocks_dct = {
                    str(tup[0]): tup[1].replace(" ", "")
                    for tup in blocks_read
                }

                rfid_model = self.new_rfid_model()
                rfid_model.from_blocks(blocks_dct)
                rfid_model_json = rfid_model.to_json()

                cache_key = self.cache.gen_key(uid_s, prefix="rfid_dict")
                self.cache.add(cache_key, rfid_model_json)

                return rfid_model_json

            return

    def rfid_write_block(self, block_num, byte_array):
        with self.use_antenna():
            uid = self.handler.prepare_loop()
            if not uid:
                return False

            uid_s = self.handler.format_block_data(uid)
            self.log_info_s(f"Card UID: {uid_s}")

            self.handler.write_single_block(uid, block_num, byte_array)

            uid = self.handler.prepare_loop()
            if uid:
                blocks_read = self.handler.read_single_block(uid, block_num)
                if blocks_read:
                    self.log_info_s(f"Block {block_num}: {blocks_read}")
                    return True

            return False

    def rfid_write_hash(self):
        with self.use_antenna():
            uid = self.handler.prepare_loop()
            if not uid:
                return

            uid_s = self.handler.format_block_data(uid)
            self.log_info_s(f"Card UID: {uid_s}")

            sha256_data_lst = self.handler.cal_blocks_sha256(uid)

            block_num = 60
            data = sha256_data_lst[:16]
            self.handler.prepare_loop()
            self.handler.write_single_block(uid, block_num, data)

            block_num = 61
            data = sha256_data_lst[16:]
            self.handler.prepare_loop()
            self.handler.write_single_block(uid, block_num, data)

    def get_tags(self):
        with self.use_antenna():
            return self.handler.read_tags()


class MMSRfid:
    """
    Printer class that controls RFID sensor
    """
    def __init__(self, config):
        self.spi = MCU_SPI_from_config(
            config=config,
            mode=0,
            pin_option="cs_pin",
            default_speed=5000000,
            share_type=None,
            cs_active_high=False
        )

        self.name = config.get_name().split()[-1]
        self.is_detecting = False
        self.is_reading = False

        self.rfid_config = RFIDConfig(config)
        self._parse_config()

        # Dynamic search state
        self.align_active = False
        self.align_found = False
        self.align_lane = None
        self.align_step = 20.0
        self.align_wait = 0.5
        self.align_max_search = 600.0
        self.align_distance_total = 0.0
        self.align_prompt_macro = None
        self.align_timer = None

        printer_adapter.register_klippy_connect(self._handle_klippy_connect)

    def _parse_config(self):
        vars_list = [
            "rfid_data_file",
            "period",
            "timeout",
        ]
        for var in vars_list:
            setattr(self, var, getattr(self.rfid_config, var))

    def _handle_klippy_connect(self):
        self._initialize_loggers()
        self._initialize_gcode()
        self._initialize_task()
        self._initialize_manager()

    def _initialize_loggers(self):
        try:
            mms_logger = printer_adapter.get_mms_logger()
        except Exception:
            mms_logger = _StandaloneLogger(f"mms_rfid_{self.name}")

        self.log_info = mms_logger.create_log_info(console_output=True)
        self.log_warning = mms_logger.create_log_warning()
        self.log_error = mms_logger.create_log_error()
        self.log_info_s = mms_logger.create_log_info(console_output=True)

    def _respond(self, msg):
        try:
            gcode = printer_adapter.get_printer().lookup_object("gcode")
            gcode.respond_info(msg)
        except Exception:
            pass

    def _reset_align_state(self):
        self.align_active = False
        self.align_found = False
        self.align_lane = None
        self.align_step = 20.0
        self.align_wait = 0.5
        self.align_max_search = 600.0
        self.align_distance_total = 0.0
        self.align_prompt_macro = None
        self.align_timer = None

    def _align_step(self, eventtime):
        reactor = printer_adapter.get_reactor()

        if not self.align_active:
            return reactor.NEVER

        if self.align_found:
            self.align_active = False
            msg = (
                f"RFID[{self.name}] align stopped on UID "
                f"after {self.align_distance_total:.1f} mm search"
            )
            self.log_info(msg)
            self._respond(msg)
            return reactor.NEVER

        if self.align_distance_total >= self.align_max_search:
            self.align_active = False
            try:
                self.detect_end()
            except Exception:
                pass
            msg = (
                f"RFID[{self.name}] align timeout after "
                f"{self.align_distance_total:.1f} mm search"
            )
            self.log_warning(msg)
            self._respond(msg)
            return reactor.NEVER

        try:
            gcode = printer_adapter.get_printer().lookup_object("gcode")
            gcode.run_script_from_command(
                f"LANE_MOVE LANE={self.align_lane} DISTANCE={self.align_step}"
            )
            self.align_distance_total += abs(self.align_step)
        except Exception as e:
            self.align_active = False
            try:
                self.detect_end()
            except Exception:
                pass
            msg = f"RFID[{self.name}] align step error: {e}"
            self.log_error(msg)
            self._respond(msg)
            return reactor.NEVER

        return eventtime + self.align_wait

    def _initialize_gcode(self):
        gcode_adapter.register_mux(
            cmd="MMS_RFID_DETECT_DEV",
            key="NAME", value=self.name,
            func=self.cmd_MMS_RFID_DETECT
        )
        gcode_adapter.register_mux(
            cmd="MMS_RFID_READ_DEV",
            key="NAME", value=self.name,
            func=self.cmd_MMS_RFID_READ
        )
        gcode_adapter.register_mux(
            cmd="MMS_RFID_WRITE_DEV",
            key="NAME", value=self.name,
            func=self.cmd_MMS_RFID_WRITE
        )
        gcode_adapter.register_mux(
            cmd="MMS_RFID_ALIGN_AND_READ",
            key="NAME", value=self.name,
            func=self.cmd_MMS_RFID_ALIGN_AND_READ
        )

    def _initialize_task(self):
        self.periodic_task = PeriodicTask()
        self.periodic_task.set_period(self.period)
        self.periodic_task.set_timeout(self.timeout)

    def _initialize_manager(self):
        self.rfid_manager = RFIDManager(self.spi)

    def _load_rfid_file(self):
        cfg_path = printer_adapter.get_klippy_configfile()
        base_dir = os.path.dirname(cfg_path)
        filename = os.path.basename(self.rfid_data_file)

        full_path = None
        json_data = None

        for root, _, files in os.walk(base_dir):
            if filename in files:
                full_path = os.path.join(root, filename)
                if self.rfid_data_file in full_path:
                    try:
                        with open(full_path, "r", encoding="utf-8") as f:
                            content = f.read()
                        json_data = json.loads(content)
                    except json.JSONDecodeError as e:
                        self.log_error(f"JSON decode error ({full_path}): {e}")
                        self._respond(f"JSON decode error ({full_path}): {e}")
                    except Exception as e:
                        self.log_error(f"open file error {full_path}: {e}")
                        self._respond(f"open file error {full_path}: {e}")

        return full_path, json_data

    def write(self):
        full_path, json_data = self._load_rfid_file()
        if not json_data:
            msg = f"RFID[{self.name}] write load rfid file failed"
            self.log_warning(msg)
            self._respond(msg)
            return False

        rfid_model = self.rfid_manager.new_rfid_model()
        rfid_model.from_dict(json_data)

        data_encode_json = rfid_model.to_json()
        msg = (
            f"RFID[{self.name}] write\n"
            "load data from file:\n"
            f"{full_path}\n"
            "data encode json:\n"
            f"{data_encode_json}"
        )
        self.log_info(msg)
        self._respond(f"RFID[{self.name}] write started using {full_path}")

        prepared_blocks = rfid_model.prepare_blocks_writing()
        for block_num, byte_array in prepared_blocks.items():
            success = self.rfid_manager.rfid_write_block(block_num, byte_array)
            if not success:
                msg = f"RFID[{self.name}] write failed at block {block_num}"
                self.log_error(msg)
                self._respond(msg)
                return False

        self.rfid_manager.rfid_write_hash()
        msg = f"RFID[{self.name}] write finished"
        self.log_info(msg)
        self._respond(msg)
        return True

    def detect_begin(self, callback):
        func = self.rfid_manager.get_uid

        try:
            is_ready = self.periodic_task.schedule(func=func, callback=callback)
            if is_ready:
                ret = self.periodic_task.start()
                if ret:
                    self.is_detecting = True
                    msg = f"RFID[{self.name}] detect initiated in the backend"
                    self.log_info(msg)
                    self._respond(msg)
                else:
                    msg = f"RFID[{self.name}] detect begin failed"
                    self.log_error(msg)
                    self._respond(msg)
            else:
                msg = f"RFID[{self.name}] detect is already running"
                self.log_warning(msg)
                self._respond(msg)
        except Exception as e:
            msg = f"RFID[{self.name}] detect_begin error:{e}"
            self.log_error(msg)
            self._respond(msg)

    def detect_end(self):
        try:
            ret = self.periodic_task.stop()
            if ret:
                self.is_detecting = False
                msg = f"RFID[{self.name}] detect terminated in the backend"
                self.log_info(msg)
                self._respond(msg)
            else:
                msg = f"RFID[{self.name}] detect is not running"
                self.log_warning(msg)
                self._respond(msg)
            return ret
        except Exception as e:
            msg = f"RFID[{self.name}] detect_end error:{e}"
            self.log_error(msg)
            self._respond(msg)

    def _handle_detected(self, data):
        if data and self.detect_end():
            uid = self.rfid_manager.to_string(block_data=data)
            msg = f"RFID[{self.name}] detect Tag uid: {uid}"
            self.log_info(msg)
            self._respond(msg)

            self.align_found = True
            self.align_active = False

            try:
                self.read_begin(callback=self._handle_read)
            except Exception as e:
                msg = f"RFID[{self.name}] auto-read start error: {e}"
                self.log_error(msg)
                self._respond(msg)

    def read_begin(self, callback):
        func = self.rfid_manager.rfid_read

        try:
            is_ready = self.periodic_task.schedule(func=func, callback=callback)
            if is_ready:
                ret = self.periodic_task.start()
                if ret:
                    self.is_reading = True
                    msg = f"RFID[{self.name}] read initiated in the backend"
                    self.log_info(msg)
                    self._respond(msg)
                else:
                    msg = f"RFID[{self.name}] read begin failed"
                    self.log_error(msg)
                    self._respond(msg)
            else:
                msg = f"RFID[{self.name}] read is already running"
                self.log_warning(msg)
                self._respond(msg)
        except Exception as e:
            msg = f"RFID[{self.name}] read_begin error:{e}"
            self.log_error(msg)
            self._respond(msg)

    def read_end(self):
        try:
            ret = self.periodic_task.stop()
            if ret:
                self.is_reading = False
                msg = f"RFID[{self.name}] read terminated in the backend"
                self.log_info(msg)
                self._respond(msg)
            else:
                msg = f"RFID[{self.name}] read is not running"
                self.log_warning(msg)
                self._respond(msg)
            return ret
        except Exception as e:
            msg = f"RFID[{self.name}] read_end error:{e}"
            self.log_error(msg)
            self._respond(msg)

    def _handle_read(self, data):
        if data and self.read_end():
            msg = f"RFID[{self.name}] read data: {data}"
            self.log_info(msg)
            self._respond(msg)

            if self.align_prompt_macro:
                try:
                    gcode = printer_adapter.get_printer().lookup_object("gcode")
                    gcode.run_script_from_command(self.align_prompt_macro)
                except Exception as e:
                    msg = f"RFID[{self.name}] prompt error: {e}"
                    self.log_error(msg)
                    self._respond(msg)

            self._reset_align_state()

    def get_tags_begin(self, callback):
        func = self.rfid_manager.get_tags

        try:
            is_ready = self.periodic_task.schedule(func=func, callback=callback)
            if is_ready:
                ret = self.periodic_task.start()
                if ret:
                    msg = f"RFID[{self.name}] get tags initiated in the backend"
                    self.log_info(msg)
                    self._respond(msg)
                else:
                    msg = f"RFID[{self.name}] get tags begin failed"
                    self.log_error(msg)
                    self._respond(msg)
            else:
                msg = f"RFID[{self.name}] get tags is already running"
                self.log_warning(msg)
                self._respond(msg)
        except Exception as e:
            msg = f"RFID[{self.name}] get_tags_begin error:{e}"
            self.log_error(msg)
            self._respond(msg)

    def get_tags_end(self):
        try:
            ret = self.periodic_task.stop()
            if ret:
                msg = f"RFID[{self.name}] get tags terminated in the backend"
                self.log_info(msg)
                self._respond(msg)
            else:
                msg = f"RFID[{self.name}] get tags is not running"
                self.log_warning(msg)
                self._respond(msg)
            return ret
        except Exception as e:
            msg = f"RFID[{self.name}] get_tags_end error:{e}"
            self.log_error(msg)
            self._respond(msg)

    def cmd_MMS_RFID_DETECT(self, gcmd):
        """
        Usage:
            MMS_RFID_DETECT_DEV NAME=mfrc522_0 SWITCH=0/1
        """
        switch = gcmd.get_int("SWITCH", 0)
        if switch == 1:
            self.detect_begin(callback=self._handle_detected)
        else:
            self.detect_end()

    def cmd_MMS_RFID_READ(self, gcmd):
        """
        Usage:
            MMS_RFID_READ_DEV NAME=mfrc522_0 SWITCH=0/1
        """
        switch = gcmd.get_int("SWITCH", 0)
        if switch == 1:
            self.read_begin(callback=self._handle_read)
        else:
            self.read_end()

    def cmd_MMS_RFID_WRITE(self, gcmd):
        """
        Usage:
            MMS_RFID_WRITE_DEV NAME=mfrc522_0
        """
        msg = f"RFID[{self.name}] write start"
        self.log_info(msg)
        self._respond(msg)
        self.write()

    def cmd_MMS_RFID_ALIGN_AND_READ(self, gcmd):
        """
        Usage:
            MMS_RFID_ALIGN_AND_READ NAME=mfrc522_0 LANE=lane1 STEP=20 WAIT=0.5 MAX_SEARCH=600 PROMPT=RFID_PROFILE_PICKER_DEV0
        """
        self.align_lane = str(gcmd.get("LANE", "lane1")).strip()
        self.align_step = gcmd.get_float("STEP", 20.0)
        self.align_wait = gcmd.get_float("WAIT", 0.5)
        self.align_max_search = gcmd.get_float("MAX_SEARCH", 600.0)
        self.align_prompt_macro = gcmd.get("PROMPT", None)

        self.align_distance_total = 0.0
        self.align_found = False
        self.align_active = True

        msg = (
            f"RFID[{self.name}] align start "
            f"lane={self.align_lane} step={self.align_step} "
            f"wait={self.align_wait} max_search={self.align_max_search}"
        )
        self.log_info(msg)
        self._respond(msg)

        try:
            self.detect_begin(callback=self._handle_detected)

            reactor = printer_adapter.get_reactor()
            self.align_timer = reactor.register_timer(
                self._align_step, reactor.monotonic() + self.align_wait
            )
        except Exception as e:
            self.align_active = False
            msg = f"RFID[{self.name}] align start error: {e}"
            self.log_error(msg)
            self._respond(msg)


def load_config(config):
    return MMSRfid(config)
