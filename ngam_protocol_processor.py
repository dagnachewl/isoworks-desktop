import sys
import isoworks_core.ngam_protocol_processor as _target

# Copy all attributes (including private ones) into the local module's namespace
self_module = sys.modules[__name__]
for name in dir(_target):
    if not name.startswith('__'):  # avoid overriding system magic attributes
        setattr(self_module, name, getattr(_target, name))
