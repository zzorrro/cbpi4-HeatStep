import asyncio
import aiohttp
import logging
from cbpi.api import *
from cbpi.api.step import CBPiStep, StepResult
from cbpi.api.dataclasses import NotificationType

logger = logging.getLogger(__name__)

@parameters([
    Property.Kettle(label="Kettle", description="Kettle to control"),
    Property.Number(label="Target Temp", description="Target temperature (°C)", configurable=True)
])
class HeatStep(CBPiStep):
    
    async def on_start(self):
        self.port = str(self.cbpi.static_config.get('port', 8000))
        
        # Получаем выбранный котёл
        self.kettle = self.get_kettle(self.props.Kettle)
        self.target_temp = float(self.props.get("Target Temp", 80))
        
        if self.kettle is None:
            self.cbpi.notify(self.name, 'Error: No kettle selected!', NotificationType.ERROR)
            logger.error("HeatStep: No kettle selected")
            await self.next()
            return
        
        # 1. Задаём уставку котлу
        self.kettle.target_temp = self.target_temp
        logger.info(f"HeatStep: Set target temp {self.target_temp}°C for kettle {self.kettle.id}")
        
        # 2. Включаем AutoMode (нагрев)
        await self.setAutoMode(True)
        logger.info(f"HeatStep: AutoMode ON for kettle {self.kettle.id}")
        
        self.cbpi.notify(
            self.name, 
            f'Heating {self.kettle.name} to {self.target_temp}°C', 
            NotificationType.SUCCESS
        )
        
        self.summary = f"Heating to {self.target_temp}°C..."
        await self.push_update()
        
        # 3. Шаг завершается СРАЗУ, нагрев продолжает работать
        await asyncio.sleep(1)
        await self.next()

    async def on_stop(self):
        """При ручной остановке выключаем нагрев"""
        if self.kettle is not None:
            await self.setAutoMode(False)
            logger.info("HeatStep: AutoMode forced OFF")
        self.summary = ""
        await self.push_update()

    async def setAutoMode(self, auto_state):
        """Управление AutoMode ТОЛЬКО для выбранного котла"""
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