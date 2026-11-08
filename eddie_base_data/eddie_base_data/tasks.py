"""EDDIE Celery task: ingest the raw inputs into PostGIS once."""
import logging

from celery import signals
from eddie.digitaltwin import setup_environment
from eddie.tasks import app

from .datasets import DATASETS
from .ingest import ingest_dataset

log = logging.getLogger(__name__)


@app.task(name="eddie_base_data.tasks.ingest_base_data")
def ingest_base_data(force: bool = False) -> dict:
    """Ingest every dataset whose source changed. A missing source does not stop the others."""
    engine = setup_environment.get_database()
    results, missing = [], []
    for dataset in DATASETS:
        if dataset.path.is_file():
            results.append(ingest_dataset(engine, dataset, force=force))
        else:
            log.error("Base data source missing: %s (table %s)", dataset.path, dataset.table)
            missing.append(str(dataset.path))
    return {"status": "incomplete" if missing else "success", "datasets": results, "missing": missing}


@signals.worker_ready.connect
def ingest_on_startup(sender, **_kwargs) -> None:
    """A fresh host gets its base tables without a manual step; otherwise this only compares hashes."""
    with sender.app.connection() as conn:
        sender.app.send_task("eddie_base_data.tasks.ingest_base_data", connection=conn)
