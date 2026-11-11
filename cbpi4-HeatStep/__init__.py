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
    """
    Фабрика callback'ов для безопасной очистки словаря.
    Проверяет, что в словаре всё ещё лежит именно эта задача,
    прежде чем удалять запись. Это предотвращает гонку состояний
    при повторном запуске HeatStep на том же котле.
    """
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
        
        # Таймер на 1 секунду для мгновенного перехода к следующему шагу
        if self.timer is None:
            self.timer = Timer(1, on_update=self.on_timer_update, on_done=self.on_timer_done)
        self.timer.start()
        
        self.kettle = self.get_kettle(self.props.Kettle)
        self.target_temp = float(self.props.get("Target Temp", 80))
        self.kettle_id = self.kettle.id if self.kettle else None
        
        if self.kettle is not None:
            # Прямое присваивание уставки - самый надежный способ передать температуру в KettleLogic
            self.kettle.target_temp = self.target_temp
            
            # Включаем нагрев
            await self.setAutoMode(True)
            self.cbpi.notify(self.name, f'Heating to {self.target_temp}°C', NotificationType.SUCCESS)
            self.summary = f"Heating to {self.target_temp}°C..."
            
            # Нативная защита через asyncio
            if self.safety_timeout_minutes > 0:
                # Отменяем предыдущую задачу, если она была
                old_task = _safety_tasks.get(self.kettle_id)
                if old_task:
                    old_task.cancel()
                    logger.info(f"HeatStep: Cancelled previous safety task for kettle {self.kettle_id}")

                # Создаем новую задачу
                task = asyncio.create_task(self._safety_shutdown(self.safety_timeout_minutes * 60))
                
                # Добавляем БЕЗОПАСНЫЙ callback, который проверяет идентичность задачи перед удалением
                task.add_done_callback(_make_cleanup(self.kettle_id, task))
                
                # Сохраняем ссылку, чтобы избежать GC
                _safety_tasks[self.kettle_id] = task
                
                logger.info(f"HeatStep: Native safety task started, will shutdown in {self.safety_timeout_minutes} min")
        else:
            self.cbpi.notify(self.name, 'Error: No kettle selected!', NotificationType.ERROR)
            self.summary = "Error: No kettle selected"
            
        await self.push_update()

    async def _safety_shutdown(self, seconds: int):
        """Нативная задача безопасности: ждет N секунд и вызывает instance.stop()"""
        try:
            await asyncio.sleep(seconds)
            
            # Используем встроенный метод шага для получения актуального объекта котла
            kettle = self.get_kettle(self.kettle_id)
            
            if kettle and kettle.instance and kettle.instance.state:
                await kettle.instance.stop()
                self.cbpi.notify('Safety System', f'SAFETY TIMEOUT: Kettle {self.kettle_id} stopped!', NotificationType.WARNING)
                logger.info(f"HeatStep: Safety timeout reached, kettle {self.kettle_id} stopped")
            else:
                logger.info(f"HeatStep: Safety timeout reached, kettle {self.kettle_id} already stopped or not found")
                
        except asyncio.CancelledError:
            logger.info(f"HeatStep: Safety task for kettle {self.kettle_id} was cancelled")
        except Exception as e:
            logger.error(f"HeatStep: Safety task error for kettle {self.kettle_id}: {e}")

    async def on_stop(self):
        await self.timer.stop()
        self.summary = ""
        
        # Отменяем задачу безопасности при ручной остановке шага
        if self.kettle_id and self.kettle_id in _safety_tasks:
            _safety_tasks[self.kettle_id].cancel()
            # Callback сам удалит запись из словаря, если это та же задача
            
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
