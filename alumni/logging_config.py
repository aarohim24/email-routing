import json
import logging


class JsonFormatter(logging.Formatter):
    def format(self, record):
        result = {'time': self.formatTime(record), 'level': record.levelname, 'event': record.getMessage()}
        for key in ('job_id', 'concern_id', 'delivery_state', 'path', 'http_status'):
            if hasattr(record, key):
                result[key] = getattr(record, key)
        if record.exc_info:
            # Exception text from external libraries can contain secrets or mail content.
            result['exception_type'] = record.exc_info[0].__name__
        return json.dumps(result)


def configure_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
