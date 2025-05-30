import asyncio
import bz2
from bson import BSON, InvalidBSON
from datetime import datetime
from dataserver.apps.util.config import load_config, get_redis_conn, get_postgres_conn, get_topic
from dataserver.apps.util.logger import make_logger
from dataserver.apps.util.redis.access import GraphManager
from dataserver.apps.util.daemon import Daemon
from dataserver.apps.util.managers.nats_manager import nats_manager  # Centralized NATSManager

config = load_config()
logger = make_logger("Catcher")

DATA_TOPIC = get_topic("publish")

nats_manager.set_server(config["nats"]["server"])

def compute_status(voltage, current):
    try:
        v = float(voltage)
        i = float(current)
    except (ValueError, TypeError):
        return "grey"

    if v == 0.0 and i > 90.0:
        return "red"      # short_circuit: high current, low voltage
    if v > 95.0 and i == 0.0:
        return "yellow"   # open_circuit: high voltage, zero current
    if 15.0 < v < 26.0:
        return "blue"     # low_voltage
    if v == 0.0 and i == 0.0:
        return "grey"     # dead_panel
    return "green"        # normal

class MITTHandler:
    def __init__(self, redis_conn):
        self.throttle_delay = config.get("daq", {}).get("throttle_delay", 0.01)
        self.backpressure_threshold = config.get("daq", {}).get("backpressure_qsize", 10)
        self.redis_conn = redis_conn
        self._stop_event = asyncio.Event()

    async def start(self):
        await nats_manager.connect()
        await nats_manager.nats.subscribe(DATA_TOPIC, cb=self.process_message)
        logger.info(f"[MITTHandler] Subscribed to topic: {DATA_TOPIC}")
        await self._stop_event.wait()

    async def stop(self):
        logger.info("[MITTHandler] Stopping...")
        self._stop_event.set()
        await nats_manager.disconnect()

    async def process_message(self, msg):
        try:
            decompressed = bz2.decompress(msg.data)
            data = BSON(decompressed).decode()
        except (InvalidBSON, OSError, ValueError) as e:
            logger.error(f"[MITTHandler] Invalid BSON: {e}")
            return

        if isinstance(data, dict) and "cache" in data:
            for item in data["cache"]:
                if isinstance(item, bytes):
                    try:
                        item = BSON(item).decode()
                    except Exception as e:
                        logger.warning(f"[MITTHandler] Skipping cached item: {e}")
                        continue
                await self.process_one_record(item)
        elif isinstance(data, dict):
            await self.process_one_record(data)
        else:
            logger.warning("[MITTHandler] Dropping unknown message format")

    async def process_one_record(self, payload: dict):
        if not isinstance(payload, dict):
            return

        raw_mac = payload.get("macaddr") or payload.get("monitor_mac")
        if isinstance(raw_mac, bytes):
            macaddr = raw_mac.decode("utf-8")
        else:
            macaddr = str(raw_mac)

        if not macaddr:
            logger.warning("[MITTHandler] No macaddr in payload")
            return

        redis_key = f"sitearray:monitor:{macaddr}"
        try:
            voltage = payload.get("Vi") or 0.0
            current = payload.get("Ii") or 0.0
            power = payload.get("Pi") or 0.0
            temperature = payload.get("temperature") or 0.0

            values = {
                "voltage": voltage,
                "current": current,
                "power": power,
                "temperature": temperature,
                "status": compute_status(voltage, current)
            }

            cleaned = {}
            for k, v in values.items():
                if isinstance(v, (str, int, float, bytes)):
                    cleaned[k] = str(v)
                else:
                    logger.warning(f"[MITTHandler] Skipping Redis field '{k}' with unsupported type: {type(v)}")

            self.redis_conn.hset(redis_key, mapping=cleaned)
            logger.info(f"[MITTHandler] Updated Redis key {redis_key}")
        except Exception as e:
            logger.error(f"[MITTHandler] Failed to write Redis key {redis_key}: {e}")

class Catcher:
    def __init__(self, site="TEST", db=1):
        self.site = site
        self.db = db
        self.redis_conn = get_redis_conn(db=self.db)
        self.graph_mgr = GraphManager(client=self.redis_conn)
        self.handler = MITTHandler(redis_conn=self.redis_conn)

    async def start(self):
        logger.info(f"[Catcher] Registering site '{self.site}' in Redis db {self.db}")
        await self.handler.start()

    async def stop(self):
        await self.handler.stop()

async def run_catcher(site="TEST", db=1):
    catcher = Catcher(site, db)
    try:
        await catcher.start()
    except asyncio.CancelledError:
        logger.info("[Catcher] Cancelled")
    finally:
        await catcher.stop()

class CatcherDaemon(Daemon):
    def run(self):
        logger.info("[CatcherDaemon] Starting")
        asyncio.run(run_catcher())

if __name__ == '__main__':
    try:
        asyncio.run(run_catcher())
    except KeyboardInterrupt:
        logger.info("[Catcher] Interrupted via keyboard")
