#!/usr/bin/env python

import json
import logging
import io
import base64
import signal
import sys
import os

import pynvml
from PIL import Image, ImageDraw, ImageFont

import plugin

pynvml.nvmlInit()

logging.basicConfig(level=logging.DEBUG)

# Get the font from the plugin directory
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_PATH = os.path.join(os.path.join(PLUGIN_DIR, "fonts"), "DejaVuSans-Bold.ttf")

try:
    value_font = ImageFont.truetype(FONT_PATH, 16)
    wattage_font = ImageFont.truetype(FONT_PATH, 12)
except OSError:
    # Fallback if the font path is wrong
    logging.warning("Font file not found, falling back to default.")
    value_font = wattage_font = ImageFont.load_default()


def handle_signal(sig, frame):
    try:
        pynvml.nvmlShutdown()
    except pynvml.NVML_ERROR_UNINITIALIZED:
        pass
    sys.exit(0)

def generate_button_img(text: str, color: str = "cyan", font: ImageFont.FreeTypeFont = value_font, width: int = 72, height: int = 72) -> str:
    """
    Display an image with the chosen GPU statistic on the Stream Deck.
    :param text: Text to display
    :param color: Color of the image (in any format that the fill parameter of Pillow supports)
    :param font: Font from ImageFont to use for the image. Uses the default "value_font" if not specified
    :param width: Image width (Stream Deck button width)
    :param height: Image height (Stream Deck button height)
    :return: Base64-encoded image to be displayed on the button
    """

    if color is None:
        color = "cyan"

    image = Image.new("RGBA", (width, height), color=(0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # Text in the center
    draw.text((width // 2, height // 2), text, fill=color, font=font, anchor="mm")

    buffered = io.BytesIO()
    image.save(buffered, format="PNG")

    img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{img_str}"

def get_gpus() -> list[dict]:
    """
    Get a list of NVIDIA GPUs currently available to the system
    :return: List of NVIDIA GPUs containing the name and UUID of the GPU
    """
    devices = []
    count = pynvml.nvmlDeviceGetCount()

    for i in range(count):
        handle = pynvml.nvmlDeviceGetHandleByIndex(i)
        name = pynvml.nvmlDeviceGetName(handle)
        uuid = pynvml.nvmlDeviceGetUUID(handle)

        devices.append({"name": name, "uuid": uuid})
    return devices

class GPUUsage(plugin.SDPlugin):
    def __init__(self, port: int, info: str, uuid: str, event: str):
        """
        :param port: WebSocket server port received from the API
        :param info: Info string received from the API
        :param uuid: The uuid of the plugin
        :param event: Event string received from the API
        """
        super().__init__(port, info, uuid, event)
        self.handles = {} # Store the NVML handles based on the UUID of the GPU, that way multiple cards should work
        self.gpus = {} # Store the GPU UUIDs for each context, in case the GPU was never set in the settings

        self.sd.loop_interval = 1 # Override the loop interval to 1 second

    def get_gpu_info(self, context: str):
        if context not in self.gpus:
            self.gpus[context] = {}

        # Get the GPU information from the settings, or use the first available one if needed
        if not self.gpus[context] and "gpu" not in self.ctxSettings[context]:
            # If no GPU has been set yet, use the first available one
            self.logger.debug("Using first available GPU, as no GPU has been set")
            gpus = get_gpus()
            if len(gpus) < 1:
                self.ShowAlert(context)
                self.logger.error("No compatible GPUs found!")
                return None
            uuid = gpus[0]["uuid"]
        elif "gpu" in self.ctxSettings[context]:
            uuid = self.ctxSettings[context]["gpu"]
        else:
            uuid = self.gpus[context]

        # Update the UUID for the context if needed
        if not self.gpus[context]:
            self.gpus[context] = uuid

        # Get the handle and update it if needed
        if uuid not in self.handles:
            try:
                self.handles[uuid] = pynvml.nvmlDeviceGetHandleByUUID(uuid)
            except pynvml.NVML_ERROR_NOT_FOUND:
                self.logger.error(f"GPU not found for UUID {uuid}")
                return None

        handle = self.handles[uuid]

        mem = pynvml.nvmlDeviceGetMemoryInfo(handle) # VRAM info
        util = pynvml.nvmlDeviceGetUtilizationRates(handle) # Utilization info
        power_mw = pynvml.nvmlDeviceGetPowerUsage(handle) # Power usage in mW
        temp = pynvml.nvmlDeviceGetTemperature(handle, 0) # Temperature
        throttle_raw = pynvml.nvmlDeviceGetCurrentClocksThrottleReasons(handle) # Anything other than 0 the GPU is throttling
        throttle = bool(throttle_raw) # Whether the GPU is throttling or not

        # Calculations
        total_gb = mem.total / 1024 ** 3
        used_gb = mem.used / 1024 ** 3
        # Determine VRAM usage as a percentage
        vram_percent = (mem.used / mem.total) * 100
        # GPU Load (Core utilization)
        gpu_usage = util.gpu

        power = power_mw / 1000.0

        return {
            "vram_total": round(total_gb, 2),
            "vram_used": round(used_gb, 2),
            "vram_usage": round(vram_percent, 1),
            "gpu_usage": gpu_usage,
            "power_usage": round(power, 2),
            "throttle": throttle,
            "temperature": temp
        }

    def get_settings(self, context: str):
        """
        Retrieves the settings associated with the given context
        :param context: Stream Deck context, provided by the WebSocket API
        :return: None
        """
        gpus = get_gpus()
        options = [{"name": gpu['name'], "value": gpu['uuid']} for gpu in gpus]

        if context not in self.ctxSettings:
            self.ctxSettings[context] = {}

        value = None

        if "gpu" in self.ctxSettings[context]:
            value = self.ctxSettings[context]["gpu"]

        settings = {"event": "getSettingsFields", "settingsFields": [{"type": "dropdown", "name": "gpu", "label": "GPUs",
                     "value": value, "options": options}]}

        #info = self.ctxInfo.get(context)

        #if info:
        #    action = info["action"].split(".")[-1]
        #    if action == "showvol":
        #        settings_ctx = self.ctxSettings.get(context)
        #        nickname = settings_ctx.get("deviceNickname")

        #        settings["settingsFields"].append({"type": "text", "name": "deviceNickname", "label": "Device Nickname",
        #                                           "value": nickname})

        payload = {"context": context, "event": "sendToPropertyInspector", "payload": settings}
        self.logger.debug(f"Sending payload to PI: {payload}")
        self.sd.socket.send(json.dumps(payload))

    def on_loop(self, context: str):
        info = self.ctxInfo.get(context)
        gpu_info = self.get_gpu_info(context)

        if not info or not gpu_info: return

        action = info["action"].split(".")[-1]

        if action == "usage":
            img = generate_button_img(f"{gpu_info["gpu_usage"]}%")
            self.SetImage(context, img)

        elif action == "vram_total":
            img = generate_button_img(f"{gpu_info['vram_total']} GB")
            self.SetImage(context, img)

        elif action == "vram_used":
            img = generate_button_img(f"{gpu_info['vram_used']} GB")
            self.SetImage(context, img)

        elif action == "vram_usage":
            img = generate_button_img(f"{gpu_info['vram_usage']}%")
            self.SetImage(context, img)

        elif action == "power_usage":
            # Use a smaller font for the wattage
            img = generate_button_img(f"{gpu_info['power_usage']} W", font=wattage_font)
            self.SetImage(context, img)

        elif action == "throttle":
            # Displays "True" or "False" based on whether the GPU is throttling or not
            # If it is throttling, the text is also red
            throttle = gpu_info["throttle"]
            color = "red" if throttle else None
            if throttle:
                text = "True"
            else:
                text = "False"
            img = generate_button_img(text, color=color)
            self.SetImage(context, img)

        elif action == "temperature":
            img = generate_button_img(f"{gpu_info['temperature']}°C")
            self.SetImage(context, img)

        else:
            return

    # Plugin overrides
    def onSendToPlugin(self, payload: dict):
        context = payload["context"]

        self.get_settings(context)

    def onPropertyInspectorDidAppear(self, payload: dict):
        context = payload["context"]

        self.get_settings(context)

if __name__ == "__main__":
    import argparse

    # Handle SIGINT and SIGTERM so that we can shut down the NVML properly
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    parser = argparse.ArgumentParser(description="Plugin parameters")
    parser.add_argument("-port", type=int)
    parser.add_argument("-info", type=str)
    parser.add_argument("-pluginUUID", type=str)
    parser.add_argument("-registerEvent", type=str)

    args = parser.parse_args()

    gpu_plugin = GPUUsage(args.port, args.info, args.pluginUUID, args.registerEvent)
    gpu_plugin.run()

    try:
        pynvml.nvmlShutdown()
    except pynvml.NVML_ERROR_UNINITIALIZED:
        pass