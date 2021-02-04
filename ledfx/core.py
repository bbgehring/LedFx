import asyncio
import logging
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor

import PySimpleGUIWx as sg

from ledfx.config import load_config, load_default_presets, save_config
from ledfx.consts import SYSTRAY_ICON
from ledfx.devices import Devices
from ledfx.effects import Effects
from ledfx.events import Events, LedFxShutdownEvent
from ledfx.http_manager import HttpServer
from ledfx.integrations import Integrations
from ledfx.utils import (
    RollingQueueHandler,
    async_fire_and_forget,
    currently_frozen,
    launch_ui,
)

_LOGGER = logging.getLogger(__name__)
if currently_frozen():
    warnings.filterwarnings("ignore")


class LedFxCore(object):
    def __init__(self, config_dir, host=None, port=None):
        self.config_dir = config_dir
        self.config = load_config(config_dir)
        self.config["default_presets"] = load_default_presets()
        host = host if host else self.config["host"]
        port = port if port else self.config["port"]
        menu_def = ["UNUSED", ["Launch Web UI", "---", "Exit"]]
        self.tray = sg.SystemTray(menu=menu_def, data_base64=SYSTRAY_ICON)

        if sys.platform == "win32":
            self.loop = asyncio.ProactorEventLoop()
        else:
            self.loop = asyncio.get_event_loop()

        self.executor = ThreadPoolExecutor()
        self.loop.set_default_executor(self.executor)
        self.loop.set_exception_handler(self.loop_exception_handler)

        self.setup_logqueue()
        self.events = Events(self)
        self.http = HttpServer(ledfx=self, host=host, port=port)
        self.exit_code = None

    def dev_enabled(self):
        return self.config["dev_mode"]

    def loop_exception_handler(self, loop, context):
        kwargs = {}
        exception = context.get("exception")
        if exception:
            kwargs["exc_info"] = (
                type(exception),
                exception,
                exception.__traceback__,
            )

        _LOGGER.error(
            "Exception in core event loop: {}".format(context["message"]),
            **kwargs,
        )

    def setup_logqueue(self):
        def log_filter(record):
            return (record.name != "ledfx.api.log") and (record.levelno >= 20)

        self.logqueue = asyncio.Queue(maxsize=100, loop=self.loop)
        logqueue_handler = RollingQueueHandler(self.logqueue)
        logqueue_handler.addFilter(log_filter)
        root_logger = logging.getLogger()
        root_logger.addHandler(logqueue_handler)

    async def flush_loop(self):
        await asyncio.sleep(0, loop=self.loop)

    def start(self, open_ui=False):
        async_fire_and_forget(self.async_start(open_ui=open_ui), self.loop)

        # Windows does not seem to handle Ctrl+C well so as a workaround
        # register a handler and manually stop the app
        if sys.platform == "win32":
            import win32api

            def handle_win32_interrupt(sig, func=None):
                self.stop()
                return True

            win32api.SetConsoleCtrlHandler(handle_win32_interrupt, 1)

        try:
            self.loop.run_forever()
        except KeyboardInterrupt:
            self.loop.call_soon_threadsafe(
                self.loop.create_task, self.async_stop()
            )
            self.loop.run_forever()
        except BaseException:
            # Catch all other exceptions and terminate the application. The loop
            # exception handler will take care of logging the actual error and
            # LedFx will cleanly shutdown.
            self.loop.run_until_complete(self.async_stop(exit_code=-1))
            pass
        finally:
            self.loop.stop()
        return self.exit_code

    async def async_start(self, open_ui=False):
        _LOGGER.info("Starting LedFx")
        await self.http.start()

        self.devices = Devices(self)
        self.effects = Effects(self)
        self.integrations = Integrations(self)

        # TODO: Deferr
        self.devices.create_from_config(self.config["devices"])
        self.integrations.create_from_config(self.config["integrations"])
        # Since we ignore duplicate WLED devices, may as well search on startup
        async_fire_and_forget(self.devices.find_wled_devices(), self.loop)

        async_fire_and_forget(
            self.integrations.activate_integrations(), self.loop
        )

        if open_ui:
            launch_ui(self.http.host, self.http.port, self.http.base_url)

        await self.flush_loop()

    def stop(self, exit_code=0):
        async_fire_and_forget(self.async_stop(exit_code), self.loop)

    def menu_handler(self):
        event = self.tray.read(500)
        if event == "Launch Web UI":
            launch_ui(self.http.host, self.http.port, self.http.base_url)
        elif event == "Exit":
            self.tray.show_message("LedFx Shutdown", "Closing LedFx")
            self.stop()

    async def async_stop(self, exit_code=0):
        if not self.loop:
            return

        print("Stopping LedFx.")

        # Fire a shutdown event and flush the loop
        self.events.fire_event(LedFxShutdownEvent())
        await asyncio.sleep(0, loop=self.loop)

        await self.http.stop()

        # Cancel all the remaining task and wait

        tasks = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
        ]
        list(map(lambda task: task.cancel(), tasks))

        # Save the configuration before shutting down
        save_config(config=self.config, config_dir=self.config_dir)

        await self.flush_loop()
        self.executor.shutdown()
        self.exit_code = exit_code
        self.loop.stop()
