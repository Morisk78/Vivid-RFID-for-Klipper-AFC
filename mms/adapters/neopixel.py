# Adapter of printer's Neopixel
#
# Copyright (C) 2025-2026 Garvey Ding <garveyding@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from .base import BaseAdapter
from .printer import printer_adapter


class NeopixelAdapter(BaseAdapter):
    def __init__(self, led_name):
        super().__init__()
        self.led_name = led_name

    def _setup_logger(self):
        mms_logger = printer_adapter.get_mms_logger()
        self.log_warning_s = mms_logger.create_log_warning(
            console_output=False)

    def _get_neopixel(self):
        return self.safe_get(self.led_name)

    def get_color_data(self):
        # neopixel.get_status() return a dict:
        # {'color_data': [(red, green, blue, white)] * led_count}
        # "led_count" is the "chain_count" set in config
        return self._get_neopixel().get_status().get("color_data")

    def update_leds(self, color_data):
        # Attempt to transmit the updated LED colors

        # New neopixel style would raise error frequently
        # try:
        #     self._get_neopixel().update_leds(
        #         led_state=color_data, print_time=None)
        # except Exception as e:
        #     self.log_warning_s(f"mms neopixel update error: {e}")

        # Use the old neopixel style, which update with mutex
        neopixel = self._get_neopixel()
        def reactor_bgfunc(eventtime):
            with self.get_mutex():
                neopixel.update_color_data(led_state=color_data)
                try:
                    neopixel.send_data()
                except Exception as e:
                    self.log_warning_s(f"mms neopixel update error: {e}")
        self.reactor.register_callback(reactor_bgfunc)


class NeopixelDispatch:
    def __init__(self):
        # key: led_name
        # val: NeopixelAdapter()
        self.np_adapter_dct = {}

    def get_adapter(self, led_name):
        if led_name in self.np_adapter_dct:
            return self.np_adapter_dct.get(led_name)

        neopixel_adapter = NeopixelAdapter(led_name)
        self.np_adapter_dct[led_name] = neopixel_adapter
        return neopixel_adapter


# Global instance for singleton
neopixel_dispatch = NeopixelDispatch()
