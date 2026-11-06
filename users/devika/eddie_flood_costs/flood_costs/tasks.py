"""EDDIE task-loader compatibility: this read-only plugin registers no tasks.

eddie.tasks imports <plugin>.tasks for every discovered eddie_* package.
The canonical source package is flood_costs; installation also exposes the
eddie_flood_costs name for the existing eddie_* discovery convention.
No Celery import, scheduling, data loading, or side effect belongs here.
"""
