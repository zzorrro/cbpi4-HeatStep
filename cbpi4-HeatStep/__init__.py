import asyncio
import aiohttp
import logging
from cbpi.api import *
from cbpi.api.step import CBPiStep, StepResult
from cbpi.api.timer import Timer
from cbpi.api.dataclasses import NotificationType

logger = logging.getLogger(__name__)

# Глобальный словарь для хранения задач безопасности по ID котла.
# Это предотвращает сборку мусора и позволяет отменять старые таймеры.
_safety_tasks = {}

@parameters([
    Property.Kettle(label="Kettle", description="Kettle to control"),
    Property.Number(label="Target Temp", description="Target temperature (°C)", configurable=True),
    Property.Number(label="Safety Timeout", description="Auto shutoff after N minutes (default 60, 0 = disabled)", configurable=True)
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
        
        self.safety_timeout_minutes = int(self.props.get("Safety Timeout", 60))
        
        # Таймер на 1 секунду для мгновенного перехода к следующему шагу
        if self.timer is None:
            self.timer = Timer(1, on_update=self.on_timer_update, on_done=self.on_timer_done)
        self.timer.start()
        
        self.kettle = self.get_kettle(self.props.Kettle)
        self.target_temp = float(self.props.get("Target Temp", 80))
        self.kettle_id = self.kettle.id if self.kettle else None
        
        if self.kettle is not None:
            # Обновляем целевую температуру через API для синхронизации с UI/БД
            await self._update_target_temp()
            
            # Включаем нагрев
            await self.setAutoMode(True)
            self.cbpi.notify(self.name, f'Heating to {self.target_temp}°C', NotificationType.SUCCESS)
            self.summary = f"Heating to {self.target_temp}°C..."
            
            # Нативная защита через asyncio
            if self.safety_timeout_minutes > 0:
                # 1. Отменяем предыдущую задачу безопасности для этого котла, если она была
                old_task = _safety_tasks.get(self.kettle_id)
                if old_task:
                    old_task.cancel()
                    logger.info(f"HeatStep: Cancelled previous safety task for kettle {self.kettle_id}")

                # 2. Создаем новую задачу и сохраняем ссылку, чтобы избежать GC
                task = asyncio.create_task(self._safety_shutdown(self.safety_timeout_minutes * 60))
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
            
            # Получаем актуальный объект котла из центрального реестра CBPi4 (безопаснее, чем self.kettle)
            kettle = self.cbpi.kettle.get(self.kettle_id)
            
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

    async def _update_target_temp(self):
        """Обновляет целевую температуру через API для корректного отображения в UI/БД"""
        try:
            url = f"http://127.0.0.1:{self.port}/kettle/{self.kettle_id}"
            payload = {"target_temp": self.target_temp}
            async with aiohttp.ClientSession() as session:
                async with session.put(url, json=payload) as response:
                    if response.status == 200:
                        logger.info(f"HeatStep: Target temp updated to {self.target_temp}°C")
        except Exception as e:
            logger.error(f"HeatStep: Error updating target temp: {e}")

    async def on_stop(self):
        await self.timer.stop()
        self.summary = ""
        
        # Отменяем задачу безопасности при ручной остановке шага
        if self.kettle_id and self.kettle_id in _safety_tasks:
            _safety_tasks[self.kettle_id].cancel()
            del _safety_tasks[self.kettle_id]
            logger.info(f"HeatStep: Safety task cancelled due to manual stop")
        
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
        
        # Также отменяем задачу при сбросе шага
        if self.kettle_id and self.kettle_id in _safety_tasks:
            _safety_tasks[self.kettle_id].cancel()
            del _safety_tasks[self.kettle_id]

    async def run(self):
        while self.running == True:
            await asyncio.sleep(1)
        return StepResult.DONE

    async def setAutoMode(self, auto_state):
        try:
            if self.kettle is None:
                return
            
            # Включение через API (аналогично нажатию кнопки в интерфейсе)
            if (self.kettle.instance is None or self.kettle.instance.state == False) and (auto_state is True):
                url = f"http://127.0.0.1:{self.port}/kettle/{self.kettle.id}/toggle"
                async with aiohttp.ClientSession() as session:
                    async with session.post(url) as response:
                        return await response.text()
            
            # Выключение через внутренний метод экземпляра (надежнее при активной сессии)
            elif self.kettle.instance is not None and self.kettle.instance.state == True and (auto_state is False):
                await self.kettle.instance.stop()
                await self.push_update()
                
        except Exception as e:
            logging.error(f"Failed to switch KettleLogic {self.kettle.id}: {e}")


def setup(cbpi):
    cbpi.plugin.register("HeatStep", HeatStep)
