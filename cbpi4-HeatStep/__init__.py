import asyncio
import aiohttp
import logging
import threading
import urllib.request
import json
from cbpi.api import *
from cbpi.api.step import CBPiStep, StepResult
from cbpi.api.timer import Timer
from cbpi.api.dataclasses import NotificationType

logger = logging.getLogger(__name__)

# Глобальный словарь для хранения активных таймеров безопасности
_safety_timers = {}

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

    def _safety_callback(self, kettle_id, port, timeout_minutes):
        """Callback для threading.Timer - выключает нагрев через заданное время"""
        try:
            # Проверяем состояние котла
            url = f"http://127.0.0.1:{port}/kettle/{kettle_id}"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode())
                state = data.get('instance', {}).get('state', False)
            
            if state:
                # Выключаем нагрев
                url = f"http://127.0.0.1:{port}/kettle/{kettle_id}/toggle"
                req = urllib.request.Request(url, method='POST')
                with urllib.request.urlopen(req, timeout=5) as response:
                    logger.info(f"HeatStep: Safety timeout triggered, kettle {kettle_id} turned OFF")
        except Exception as e:
            logger.error(f"HeatStep: Error in safety callback: {e}")
        finally:
            # Удаляем таймер из глобального словаря
            _safety_timers.pop(kettle_id, None)

    async def on_start(self):
        self.port = str(self.cbpi.static_config.get('port', 8000))
        self.step_completed_normally = False
        
        # Параметры безопасности
        self.safety_timeout_minutes = int(self.props.get("Safety Timeout", 40))
        
        # Инициализируем таймер на 1 секунду для перехода к следующему шагу
        if self.timer is None:
            self.timer = Timer(1, on_update=self.on_timer_update, on_done=self.on_timer_done)
        self.timer.start()
        
        # Получаем параметры
        self.kettle = self.get_kettle(self.props.Kettle)
        self.target_temp = float(self.props.get("Target Temp", 80))
        
        if self.kettle is not None:
            self.kettle_id = self.kettle.id
            self.kettle.target_temp = self.target_temp
            await self.setAutoMode(True)
            self.cbpi.notify(self.name, f'Heating to {self.target_temp}°C', NotificationType.SUCCESS)
            self.summary = f"Heating to {self.target_temp}°C..."
            
            # Запускаем защитный таймер через threading.Timer
            if self.safety_timeout_minutes > 0:
                # Отменяем старый таймер для этого котла, если есть
                if self.kettle_id in _safety_timers:
                    _safety_timers[self.kettle_id].cancel()
                
                # Создаём новый таймер
                timer = threading.Timer(
                    self.safety_timeout_minutes * 60,
                    self._safety_callback,
                    args=[self.kettle_id, self.port, self.safety_timeout_minutes]
                )
                timer.daemon = True
                timer.start()
                
                # Сохраняем в глобальный словарь
                _safety_timers[self.kettle_id] = timer
                
                logger.info(f"HeatStep: Safety timer started for {self.safety_timeout_minutes} min")
        else:
            self.cbpi.notify(self.name, 'Error: No kettle selected!', NotificationType.ERROR)
            self.summary = "Error: No kettle selected"
            
        await self.push_update()

    async def on_stop(self):
        await self.timer.stop()
        self.summary = ""
        
        # Отменяем таймер безопасности при ручной остановке
        if hasattr(self, 'kettle_id') and self.kettle_id in _safety_timers:
            _safety_timers[self.kettle_id].cancel()
            del _safety_timers[self.kettle_id]
            logger.info("HeatStep: Safety timer cancelled due to manual stop")
        
        # Выключаем AutoMode только при ручной остановке
        if self.kettle is not None and not self.step_completed_normally:
            await self.setAutoMode(False)
            logger.info("HeatStep: AutoMode forced OFF")
        elif self.step_completed_normally:
            logger.info("HeatStep: Step finished normally. AutoMode left ON.")
            
        await self.push_update()

    async def reset(self):
        self.timer = Timer(1, on_update=self.on_timer_update, on_done=self.on_timer_done)
        self.step_completed_normally = False
        
        # Отменяем таймер безопасности при сбросе
        if hasattr(self, 'kettle_id') and self.kettle_id in _safety_timers:
            _safety_timers[self.kettle_id].cancel()
            del _safety_timers[self.kettle_id]

    async def run(self):
        while self.running == True:
            await asyncio.sleep(1)
        return StepResult.DONE

    async def setAutoMode(self, auto_state):
        try:
            if self.kettle is None:
                return
            
            if (self.kettle.instance is None or self.kettle.instance.state == False) and (auto_state is True):
                url = "http://127.0.0.1:" + self.port + "/kettle/" + self.kettle.id + "/toggle"
                async with aiohttp.ClientSession() as session:
                    async with session.post(url) as response:
                        return await response.text()
            
            elif self.kettle.instance is not None and self.kettle.instance.state == True and (auto_state is False):
                await self.kettle.instance.stop()
                await self.push_update()
                
        except Exception as e:
            logging.error(f"Failed to switch KettleLogic {self.kettle.id}: {e}")


def setup(cbpi):
    cbpi.plugin.register("HeatStep", HeatStep)
