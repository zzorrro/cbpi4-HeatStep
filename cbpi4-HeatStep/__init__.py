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
    Property.Number(label="Duration", description="Heating duration in minutes (0 = until manual stop)", configurable=True)
])
class HeatStep(CBPiStep):

    async def on_timer_done(self, timer):
        self.summary = ""
        self.cbpi.notify(self.name, 'Heating duration completed. AutoMode left ON.', NotificationType.SUCCESS)
        await self.next()

    async def on_timer_update(self, timer, seconds):
        self.summary = Timer.format_time(seconds)
        await self.push_update()

    async def on_start(self):
        self.port = str(self.cbpi.static_config.get('port', 8000))
        
        self.kettle = self.get_kettle(self.props.Kettle)
        self.target_temp = float(self.props.get("Target Temp", 80))
        self.duration_minutes = int(self.props.get("Duration", 40))
        self.duration_seconds = self.duration_minutes * 60
        
        if self.kettle is not None:
            self.kettle.target_temp = self.target_temp
            await self.setAutoMode(True)
            self.cbpi.notify(self.name, f'Heating to {self.target_temp}°C for {self.duration_minutes} min', NotificationType.SUCCESS)
            
            if self.duration_seconds > 0:
                self.timer = Timer(self.duration_seconds, on_update=self.on_timer_update, on_done=self.on_timer_done)
                self.timer.start()
                self.summary = Timer.format_time(self.duration_seconds)
            else:
                self.timer = None
                self.summary = f"Heating to {self.target_temp}°C (manual stop)"
        else:
            self.cbpi.notify(self.name, 'Error: No kettle selected!', NotificationType.ERROR)
            self.summary = "Error"
            
        await self.push_update()

    async def on_stop(self):
        if self.timer is not None:
            await self.timer.stop()
        self.summary = ""
        
        if self.kettle is not None:
            await self.setAutoMode(False)
            logger.info("HeatStep: AutoMode forced OFF")
            
        await self.push_update()

    async def reset(self):
        if self.duration_seconds > 0:
            self.timer = Timer(self.duration_seconds, on_update=self.on_timer_update, on_done=self.on_timer_done)

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
