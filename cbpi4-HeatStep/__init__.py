import asyncio
import aiohttp
import logging
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

    async def safety_timeout(self):
        """Фоновая задача: выключает нагрев через заданное время для безопасности"""
        try:
            await asyncio.sleep(self.safety_timeout_seconds)
            
            # Проверяем, что котёл всё ещё греется (иначе уже выключили)
            if (self.kettle is not None and 
                self.kettle.instance is not None and 
                self.kettle.instance.state == True):
                
                await self.setAutoMode(False)
                self.cbpi.notify(
                    self.name, 
                    f'Safety timeout: heating stopped after {self.safety_timeout_minutes} min', 
                    NotificationType.WARNING
                )
                logger.info(f"HeatStep: Safety timeout triggered after {self.safety_timeout_minutes} min")
        except asyncio.CancelledError:
            # Задача отменена (ручная остановка или reset) — это нормально
            logger.info("HeatStep: Safety timeout task cancelled")
        except Exception as e:
            logger.error(f"HeatStep: Error in safety timeout: {e}")

    async def on_start(self):
        self.port = str(self.cbpi.static_config.get('port', 8000))
        self.step_completed_normally = False
        
        # Параметры безопасности
        self.safety_timeout_minutes = int(self.props.get("Safety Timeout", 60))
        self.safety_timeout_seconds = self.safety_timeout_minutes * 60
        self.safety_task = None
        
        # 1. Инициализируем таймер на 1 секунду для автоматического перехода
        if self.timer is None:
            self.timer = Timer(1, on_update=self.on_timer_update, on_done=self.on_timer_done)
        self.timer.start()
        
        # 2. Получаем параметры
        self.kettle = self.get_kettle(self.props.Kettle)
        self.target_temp = float(self.props.get("Target Temp", 80))
        
        if self.kettle is not None:
            self.kettle.target_temp = self.target_temp
            await self.setAutoMode(True)
            self.cbpi.notify(self.name, f'Heating to {self.target_temp}°C', NotificationType.SUCCESS)
            self.summary = f"Heating to {self.target_temp}°C..."
            
            # 3. Запускаем защитный таймаут, если он задан (>0)
            if self.safety_timeout_seconds > 0:
                self.safety_task = asyncio.create_task(self.safety_timeout())
                logger.info(f"HeatStep: Safety timeout set for {self.safety_timeout_minutes} min")
        else:
            self.cbpi.notify(self.name, 'Error: No kettle selected!', NotificationType.ERROR)
            self.summary = "Error: No kettle selected"
            
        await self.push_update()

    async def on_stop(self):
        await self.timer.stop()
        self.summary = ""
        
        # Отменяем задачу защитного таймаута
        if self.safety_task is not None:
            self.safety_task.cancel()
            self.safety_task = None
        
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
        # Отменяем старый таймаут при сбросе
        if self.safety_task is not None:
            self.safety_task.cancel()
            self.safety_task = None

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
