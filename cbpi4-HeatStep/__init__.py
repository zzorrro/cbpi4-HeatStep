import asyncio
import aiohttp
import logging
import threading
from cbpi.api import *
from cbpi.api.step import CBPiStep, StepResult
from cbpi.api.timer import Timer
from cbpi.api.dataclasses import NotificationType

logger = logging.getLogger(__name__)

@parameters([
    Property.Kettle(label="Kettle", description="Kettle to control"),
    Property.Number(label="Target Temp", description="Target temperature (°C)", configurable=True),
    Property.Number(label="Safety Timeout", description="Auto shutoff after N minutes (0 = disabled)", configurable=True)
])
class HeatStep(CBPiStep):

    async def on_timer_done(self, timer):
        self.summary = ""
        self.step_completed_normally = True
        await self.next()

    async def on_timer_update(self, timer, seconds):
        self.summary = Timer.format_time(seconds)
        await self.push_update()

    def _safety_callback(self):
        """Синхронный callback для threading.Timer — выключает нагрев через HTTP API"""
        try:
            logger.info(f"HeatStep: Safety timeout elapsed, turning OFF kettle {self.kettle_id}")
            
            # Синхронный HTTP-запрос через urllib (без aiohttp, т.к. мы в другом потоке)
            import urllib.request
            url = f"http://127.0.0.1:{self.port}/kettle/{self.kettle_id}/toggle"
            req = urllib.request.Request(url, method='POST')
            with urllib.request.urlopen(req, timeout=5) as response:
                result = response.read().decode()
                logger.info(f"HeatStep: Safety timeout toggle response: {result}")
            
            # Уведомление через cbpi (поточно-безопасно)
            self.cbpi.notify(
                self.name, 
                f'Safety timeout: heating stopped after {self.safety_timeout_minutes} min', 
                NotificationType.WARNING
            )
            logger.info(f"HeatStep: Safety timeout triggered successfully")
            
        except Exception as e:
            logger.error(f"HeatStep: Error in safety timeout: {e}")
            import traceback
            logger.error(traceback.format_exc())

    async def on_start(self):
        self.port = str(self.cbpi.static_config.get('port', 8000))
        self.step_completed_normally = False
        
        # Параметры безопасности
        self.safety_timeout_minutes = int(self.props.get("Safety Timeout", 40))
        self.safety_timeout_seconds = self.safety_timeout_minutes * 60
        self.safety_timer = None
        
        # Инициализируем таймер на 1 секунду для перехода к следующему шагу
        if self.timer is None:
            self.timer = Timer(1, on_update=self.on_timer_update, on_done=self.on_timer_done)
        self.timer.start()
        
        # Получаем параметры
        self.kettle = self.get_kettle(self.props.Kettle)
        self.target_temp = float(self.props.get("Target Temp", 80))
        
        if self.kettle is not None:
            # Сохраняем ID котла и порт для фонового потока
            self.kettle_id = self.kettle.id
            self.kettle.target_temp = self.target_temp
            await self.setAutoMode(True)
            self.cbpi.notify(self.name, f'Heating to {self.target_temp}°C', NotificationType.SUCCESS)
            self.summary = f"Heating to {self.target_temp}°C..."
            
            # Запускаем защитный таймер через threading (не зависит от жизненного цикла шага)
            if self.safety_timeout_seconds > 0:
                self.safety_timer = threading.Timer(
                    self.safety_timeout_seconds, 
                    self._safety_callback
                )
                self.safety_timer.daemon = True  # Поток завершится вместе с программой
                self.safety_timer.start()
                logger.info(f"HeatStep: Safety timer started for {self.safety_timeout_minutes} min (threading)")
        else:
            self.cbpi.notify(self.name, 'Error: No kettle selected!', NotificationType.ERROR)
            self.summary = "Error: No kettle selected"
            
        await self.push_update()

    async def on_stop(self):
        await self.timer.stop()
        self.summary = ""
        
        # Отменяем threading.Timer при ручной остановке
        if self.safety_timer is not None:
            self.safety_timer.cancel()
            self.safety_timer = None
            logger.info("HeatStep: Safety timer cancelled due to manual stop")
        
        # Выключаем AutoMode только при ручной остановке
        if self.kettle is not None and not self.step_completed_normally:
            await self.setAutoMode(False)
            logger.info("HeatStep: AutoMode forced OFF due to manual stop")
        elif self.step_completed_normally:
            logger.info("HeatStep: Step finished normally. AutoMode left ON.")
            
        await self.push_update()

    async def reset(self):
        self.timer = Timer(1, on_update=self.on_timer_update, on_done=self.on_timer_done)
        self.step_completed_normally = False
        if self.safety_timer is not None:
            self.safety_timer.cancel()
            self.safety_timer = None

    async def run(self):
        while self.running == True:
            await asyncio.sleep(1)
        return StepResult.DONE

    async def setAutoMode(self, auto_state):
        try:
            if self.kettle is None:
                return
            
            # ВКЛЮЧЕНИЕ
            if (self.kettle.instance is None or self.kettle.instance.state == False) and (auto_state is True):
                url = "http://127.0.0.1:" + self.port + "/kettle/" + self.kettle.id + "/toggle"
                async with aiohttp.ClientSession() as session:
                    async with session.post(url) as response:
                        return await response.text()
            
            # ВЫКЛЮЧЕНИЕ
            elif self.kettle.instance is not None and self.kettle.instance.state == True and (auto_state is False):
                await self.kettle.instance.stop()
                await self.push_update()
                
        except Exception as e:
            logging.error(f"Failed to switch KettleLogic {self.kettle.id}: {e}")


def setup(cbpi):
    cbpi.plugin.register("HeatStep", HeatStep)
