with open("siam_processor/views/main_window.py", "r") as f:
    content = f.read()
import_block = """
def global_exception_handler(exc_type, exc_value, exc_traceback):
    import logging
    logging.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))

import sys
sys.excepthook = global_exception_handler
"""
content = content.replace("import sys\n", "import sys\n" + import_block)
with open("siam_processor/views/main_window.py", "w") as f:
    f.write(content)
