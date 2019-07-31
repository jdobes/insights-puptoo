import traceback

from collections import deque
from prometheus_client import start_http_server
from kafka.errors import KafkaError

import process
import tracker

from utils import config, metrics, puptoo_logging
from mq import consume, produce

logger = puptoo_logging.initialize_logging()

produce_queue = deque([])

def start_prometheus():
    start_http_server(config.PROMETHEUS_PORT)


def get_extra(account="unknown", request_id="unknown"):
    return {"account": account, "request_id": request_id}


def main():

    logger.info("Starting Puptoo Service")

    logger.info("Using LOG_LEVEL: %s", config.LOG_LEVEL)
    logger.info("Using LOG_GROUP: %s", config.LOG_GROUP)
    logger.info("Using BOOTSTRAP_SERVERS: %s", config.BOOTSTRAP_SERVERS)
    logger.info("Using GROUP_ID: %s", config.GROUP_ID)
    logger.info("Using INVENTORY_TOPIC: %s", config.INVENTORY_TOPIC)
    logger.info("Using TRACKER_TOPIC: %s", config.TRACKER_TOPIC)
    logger.info("Using DISABLE_PROMETHEUS: %s", config.DISABLE_PROMETHEUS)
    logger.info("Using PROMETHEUS_PORT: %s", config.PROMETHEUS_PORT)

    if not config.DISABLE_PROMETHEUS:
        logger.info("Starting Puptoo Prometheus Server")
        start_prometheus()

    consumer = consume.init_consumer()
    producer = produce.init_producer()

    while True:
        for data in consumer:
            msg = data.value
            extra = get_extra(msg.get("account"), msg.get("request_id"))
            produce_queue.append(tracker.tracker_msg(extra, "received", "Received message"))
            metrics.msg_count.inc()
            facts = process.extraction(msg, extra)
            if facts.get("error"):
                metrics.extract_failure.inc()
                produce_queue.append(tracker.tracker_msg(extra, "failure", "Unable to extract facts"))
                continue
            logger.debug("extracted facts from message for %s", extra["request_id"])
            inv_msg = {"data": {**msg, **facts}}
            produce_queue.append(tracker.tracker_msg(extra, "processing", "Successfully extracted facts"))
            produce_queue.append({"topic": config.INVENTORY_TOPIC, "msg": inv_msg, "extra": extra})
            metrics.msg_processed.inc()
            item = produce_queue.popleft()
            try:
                producer.send(item["topic"], value=item["msg"])
                logger.debug("sent msg to %s for request_id: %s", config.INVENTORY_TOPIC, extra["request_id"])
            except KafkaError:
                logger.exception("Failed to produce message.Placing back on queue: %s", item["extra"]["request_id"])
            if item["topic"] == config.INVENTORY_TOPIC:
                try:
                    tracker_msg = tracker.tracker_msg(item["extra"], "success", "Sent to inventory")
                    producer.send(tracker_msg["topic"], value=tracker_msg["msg"])
                except KafkaError:
                    logger.exception("Failed to send payload tracker message for request %s", tracker_msg["extras"]["request_id"])
            metrics.msg_produced.inc()

        producer.flush()

if __name__ == "__main__":
    try:
        main()
    except Exception:
        the_error = traceback.format_exc()
        logger.error(f"Puptoo failed with Error: {the_error}")
