from PyQt5.QtCore import QObject, pyqtSignal, QThread
from siam_processor.models.processor_model import ProcessorModel
from siam_processor.workers.worker import Worker
import logging
import pandas as pd

class ProcessorViewModel(QObject):
    # Signals for UI updates
    processingStarted = pyqtSignal()
    processingFinished = pyqtSignal(object, object, object, object, object, object)
    processingError = pyqtSignal(str)
    progressUpdated = pyqtSignal(str)

    def __init__(self, model: ProcessorModel, parent=None):
        super().__init__(parent)
        self.model = model
        self._thread = None
        self._worker = None

    def start_processing(self, data_file, standards_data, instrument, config, corrections, roles, methods, isotopes, include_ignored, preloaded_df):
        """
        Kicks off the background worker to process data.
        """
        self.processingStarted.emit()
        self.progressUpdated.emit("Status: Preparing worker...")

        self._thread = QThread()
        # Pass data to worker
        self._worker = Worker(
            data_file=data_file,
            standards_data=standards_data,
            instrument=instrument,
            config=config,
            corrections=corrections,
            roles=roles,
            methods=methods,
            isotopes=isotopes,
            include_ignored=include_ignored,
            preloaded_df=preloaded_df
        )

        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)

        self._worker.error.connect(self._on_worker_error)
        self._worker.progress.connect(self.progressUpdated.emit)

        self._thread.start()

    def _on_worker_finished(self, injection_data, analysis_data, validation_results, memory_fits, drift_fits, mem_factors):
        """
        Callback when worker successfully finishes. Updates the Model.
        """
        # Store in Model
        self.model.injection_data = injection_data
        self.model.analysis_data = analysis_data
        self.model.memory_fits = memory_fits
        self.model.drift_fits = drift_fits
        # Validation results usually trigger a UI summary, could be held in Model or passed through
        
        self.progressUpdated.emit("Status: Processing complete.")
        self.processingFinished.emit(injection_data, analysis_data, validation_results, memory_fits, drift_fits, mem_factors)

    def _on_worker_error(self, err: str):
        self._thread.quit()
        self.processingError.emit(err)
