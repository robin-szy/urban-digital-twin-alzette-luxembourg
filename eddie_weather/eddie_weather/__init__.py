"""eddie_weather: rain per sub-catchment and the GR4J river flow of the whole Alzette.

Lagloire's backend module of the Alzette digital twin. It computes ahead (Celery tasks on a
schedule), stores every value as place, time, value, unit, quality and source, and serves
them through a small data API. It changes nothing in the other modules; they connect to
its API when they choose to.
"""
