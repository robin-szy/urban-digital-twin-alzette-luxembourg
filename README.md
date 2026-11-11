# The Story of the Alzette Twin

In our university project, we built a digital twin for the Alzette river in Luxembourg. But how did we get there and what decisions did we make along the way? We will illuminate this in the following series of chapters.

---

## 0. Who are we?

We are the **river Alzette**. We flow through Luxembourg, through towns, fields and industry. Sometimes we flood and we damage. Sometimes others harm us. We want to recompensate those who we harm and charge those who harm us. To do that, we need to understand ourselves and our surroundings. That led to the first question:

> *Where are we, and what does our world look like?*

---

## 1. The ground we flow on: terrain

Water flows downhill. Before we can say anything about floods, rain or runoff, we need to know the shape of the land that surrounds us.

> *Why terrain first?*

### Why this data?
_TODO_

### Data source
_TODO_ (which DEM, resolution, provider, year)

### Choices we made
_TODO_ (e.g. why this DEM type and not another)

### What it shows in the twin
_TODO_ (screenshot / layer name)

### Limits
_TODO_

**Contributor(s):** _TODO_ · **Code:** _TODO_

---

## 2. Where we overflow: flood zones

Knowing the terrain, the next question is obvious: where does the river go when it leaves its bed?

> *Which areas are at risk when we flood?*

### Why this data?
_TODO_

### Data source
_TODO_ (e.g. HQ100 flood hazard maps)

### Choices we made
_TODO_

### What it shows in the twin
_TODO_

### Limits
_TODO_

**Contributor(s):** _TODO_ · **Code:** _TODO_

---

## 3. Who lives with us: buildings and land cover

A flood on an empty field or a park is a different story from a flood in a town. Moreover, the buildings and land cover affects the flow of water and the runoff. To understand the impact of floods, we need to know what is around us.

> *What and who is affected when we flood? What and who changes the amount of water we would receive naturally?*

### Why this data?
_TODO_

### Data source
_TODO_ (building footprints, heights, land cover)

### Choices we made
_TODO_ (2D footprints → 3D buildings, etc.)

### What it shows in the twin
_TODO_

### Limits
_TODO_

**Contributor(s):** _TODO_ · **Code:** _TODO_ (`eddie_buildings/`)

---

## 4. Why 2D was not enough: the framework

So far everything fits in QGIS. But a river is not a static map. We move, we rise and fall, we react to weather. We are **4D: 3D plus time**.

> *How do we bring all of this together, in 3D and in real time?*

### Why a framework?
_TODO_ (what QGIS could not do)

### Why EDDIE?
_TODO_ (ready-made architecture, Cesium 3D frontend, PostGIS / GeoServer, automatic publishing)

### What we had to change
_TODO_ (New Zealand coordinate system, ingestion, storage, frontend)

### What it gives us
_TODO_

### Limits
_TODO_

**Contributor(s):** Robin-szy · **Code:** _TODO_ (`EDDIE/`, `eddie_alzette_ingest/`)

---

## 5. Where our water comes from: rain

Floods do not start in the river. They start in the sky. To understand why our level rises, we first have to see the rain falling in our watersheds.

Also, rain gauges measure at single points, but rain falls everywhere. We need a continuous picture of how much rain hits each part of our watersheds.

> *Why rain? How much rain falls between the stations?*

### Why this data?
_TODO_

### Data source
_TODO_ (e.g. ASTA hourly precipitation stations)

### Choices we made
_TODO_ (update frequency, which stations, storage)

### What it shows in the twin
_TODO_

### Limits
_TODO_

**Contributor(s):** Lagloire, Robin-szy · **Code:** _TODO_ (`data_sources/`, `eddie_alzette_ingest/`, `eddie_precip_interpolation/`)

---

## 6. Our effect: river water level

Rain is the cause, the water level is the effect. To understand our state and how we might harm others, we need to measure our volume.

> *How high are we right now, and how did we get here?*

### Why this data?
_TODO_

### Data source
_TODO_ (gauging stations, provider, frequency)

### Choices we made
_TODO_

### What it shows in the twin
_TODO_

### Limits
_TODO_

**Contributor(s):** Lagloire · **Code:** _TODO_ (`gauge_collector/`, `eddie_water_level/`)

---

## 7. Remembering and replaying: time

A twin that only shows "now" cannot learn. We want to revisit past floods and, one day, simulate future ones.

> *What happened last time it rained like this?*

### Why time series?
_TODO_

### What we built
_TODO_ (time-based charts, replay)

### Choices we made
_TODO_

### What it shows in the twin
_TODO_

### Limits
_TODO_

**Contributor(s):** Lagloire · **Code:** _TODO_

---

## 8. Focusing on a place: area of interest

The catchment is large. A user (or a module) usually cares about one place: a town, a street, a factory. Simulating all of Luxembourg reduced the performance of the twin and hence negatively influences the user experience.

> *How do we zoom into one area and ask questions about it?*

### Why an AOI module?
_TODO_

### What we built
_TODO_

### Choices we made
_TODO_

### What it shows in the twin
_TODO_

### Limits
_TODO_

**Contributor(s):** Robin · **Code:** _TODO_ (`eddie_aoi/`)

---

## 9. Who harms us?

Now we can see ourselves and our surroundings in 3D and over time. That leads to responsibility. Some take our water, seal the ground around us, or pollute us. They should pay their share.

> *Who burdens us, and how much should they pay?*

### Why this question?
_TODO_

### Models
_TODO_ (e.g. water fee estimate for non-residential buildings)

### Choices we made
_TODO_

### What it shows in the twin
_TODO_

### Limits
_TODO_

**Contributor(s):** Pegah · **Code:** _TODO_

---

## 10. Who do we harm?

Responsibility goes both ways. When we flood, we damage homes, businesses and fields that never did us any harm. If others pay for burdening us, those we damage should be compensated.

> *Whom do we damage when we flood, and how much?*

### Why this question?
_TODO_

### Models
_TODO_ (e.g. flood damage per building, exposure in flood zones)

### Choices we made
_TODO_

### What it shows in the twin
_TODO_

### Limits
_TODO_

**Contributor(s):** Devika · **Code:** _TODO_

---

## 11. Where we are, and what comes next

### What works today
_TODO_

### What is still missing
_TODO_

### Next steps
_TODO_

---

## Team

| Name | Role / chapters |
|------|-----------------|
| _TODO_ | _TODO_ |