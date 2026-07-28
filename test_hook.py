import sys, logging
logging.basicConfig(level=logging.INFO)
def global_exception_handler(exc_type, exc_value, exc_traceback):
    logging.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))
sys.excepthook = global_exception_handler
raise ValueError("TEST ERROR")
