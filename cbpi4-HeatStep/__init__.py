import asyncio
import aiohttp
import logging
from cbpi.api import *
from cbpi.api.step import CBPiStep, StepResult
from cbpi.api.timer import Timer
from cbpi.api.dataclasses import NotificationType

logger = logging.getLogger(__name__)

# Глобальный словарь для хранения задач безопасности
_safety_tasks = {}

def _make_cleanup(kettle_id, task):
    def cleanup(_):
        if _safety_tasks.get(kettle_id) is task:
            _safety_tasks.pop(kettle_id, None)
            logger.info(f"HeatStep: Safety task for kettle {kettle_id} removed from registry")
    return cleanup

@parameters([
    Property.Kettle(label="Kettle", description="Kettle to control"),
    Property.Number(label="Target Temp", description="Target temperature (°C)", configurable=True),
    Property.Number(label="Safety Timeout", description="Auto shutoff after N minutes (default 90, 0 = disabled)", configurable=True)
])
class HeatStep(CBPiStep):

    async def on_timer_done(self, timer):
        self.summary = ""
        self.step_completed_normally = True
        await self.next()

    async def on_timer_update(self, timer, seconds):
        self.summary = Timer.format_time(seconds)
        await self.push_update()

    async def on_start(self):
        self.port = str(self.cbpi.static_config.get('port', 8000))
        self.step_completed_normally = False
        
        self.safety_timeout_minutes = int(self.props.get("Safety Timeout", 90))
        
        if self.timer is None:
            self.timer = Timer(1, on_update=self.on_timer_update, on_done=self.on_timer_done)
        self.timer.start()
        
        self.kettle = self.get_kettle(self.props.Kettle)
        self.target_temp = float(self.props.get("Target Temp", 80))
        self.kettle_id = self.kettle.id if self.kettle else None
        
        if self.kettle is not None:
            self.kettle.target_temp = self.target_temp
            
            await self.setAutoMode(True)
            self.cbpi.notify(self.name, f'Heating to {self.target_temp}°C', NotificationType.SUCCESS)
            self.summary = f"Heating to {self.target_temp}°C..."
            
            if self.safety_timeout_minutes > 0:
                old_task = _safety_tasks.get(self.kettle_id)
                if old_task:
                    old_task.cancel()

                task = asyncio.create_task(self._safety_shutdown(self.safety_timeout_minutes * 60))
                task.add_done_callback(_make_cleanup(self.kettle_id, task))
                _safety_tasks[self.kettle_id] = task
                
                logger.info(f"SAFETY TIMER: Started for {self.safety_timeout_minutes} minutes on kettle {self.kettle_id}")
        else:
            self.cbpi.notify(self.name, 'Error: No kettle selected!', NotificationType.ERROR)
            self.summary = "Error: No kettle selected"
            
        await self.push_update()

    async def _safety_shutdown(self, seconds: int):
        """Усиленная задача безопасности с двойной защитой"""
        try:
            await asyncio.sleep(seconds)
            
            # Уровень 0: Получаем актуальный объект котла
            kettle = self.get_kettle(self.kettle_id)
            if not kettle:
                logger.error(f"SAFETY TIMER: Kettle {self.kettle_id} NOT FOUND!")
                return

            # Уровень 1: Останавливаем логику котла
            if kettle.instance and kettle.instance.state:
                await kettle.instance.stop()
                logger.warning(f"SAFETY TIMER: Kettle logic stopped for {self.kettle_id}")

            # Уровень 2: Прямое выключение актора нагревателя (ДВОЙНАЯ ЗАЩИТА)
            if kettle.heater:
                # ИСПРАВЛЕНО: используем find_by_id, как в стандартном API CBPi4
                heater_actor = self.cbpi.actor.find_by_id(kettle.heater)
                if heater_actor:
                    await heater_actor.off()
                    logger.warning(f"SAFETY TIMER: Heater actor '{kettle.heater}' forced OFF directly")
                else:
                    logger.error(f"SAFETY TIMER: Could not find heater actor '{kettle.heater}'")

            self.cbpi.notify('SAFETY SYSTEM', f'TIMEOUT REACHED: Kettle {self.kettle_id} FORCED STOP!', NotificationType.WARNING)
            logger.warning(f"SAFETY TIMER: Execution complete. Kettle {self.kettle_id} should be OFF.")
                
        except asyncio.CancelledError:
            logger.info(f"SAFETY TIMER: Task for kettle {self.kettle_id} was cancelled")
        except Exception as e:
            logger.error(f"SAFETY TIMER: Critical error for kettle {self.kettle_id}: {e}", exc_info=True)

    async def on_stop(self):
        await self.timer.stop()
        self.summary = ""
        
        # Отменяем safety-задачу ТОЛЬКО при ручной остановке
        if self.kettle_id and not self.step_completed_normally and self.kettle_id in _safety_tasks:
            _safety_tasks[self.kettle_id].cancel()
            logger.info("HeatStep: Safety task cancelled due to MANUAL stop")

        # Выключаем AutoMode ТОЛЬКО при ручной остановке
        if self.kettle is not None and not self.step_completed_normally:
            await self.setAutoMode(False)
            logger.info("HeatStep: AutoMode forced OFF (manual stop)")
        elif self.step_completed_normally:
            logger.info("HeatStep: Step finished normally. AutoMode and Safety Timer left ON.")
            
        await self.push_update()

    async def reset(self):
        self.timer = Timer(1, on_update=self.on_timer_update, on_done=self.on_timer_done)
        self.step_completed_normally = False
        if self.kettle_id and self.kettle_id in _safety_tasks:
            _safety_tasks[self.kettle_id].cancel()

    async def run(self):
        while self.running == True:
            await asyncio.sleep(1)
        return StepResult.DONE

    async def setAutoMode(self, auto_state):
        try:
            if self.kettle is None:
                return
            
            if (self.kettle.instance is None or self.kettle.instance.state == False) and (auto_state is True):
                url = f"http://127.0.0.1:{self.port}/kettle/{self.kettle.id}/toggle"
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
