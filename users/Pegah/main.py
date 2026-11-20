import os
from src import config
from src.aoi_processor import generate_aoi_and_filter_buildings
from src.population_model import calculate_population_and_heights

from src.osm_categorizer import enrich_with_osm_industrial_data

from src.simple_model_first_scenario import non_residential_water_allocation
from src.timeseries_model_first_scenario import period_fees
from src.simple_model_second_scenario import process_rainwater_impact
from src.simple_model_third_scenario import process_buildings_flood_risk
from src.simple_model_fourth_scenario import process_water_quality_charge
from src.simple_model_fifth_scenario import process_building_population_water_consumption

from src.exporters import export_results

# Raw inputs under data/raw/ (paths relative to the project root, as in config.py).
KILOMETRAGE_PATH = "data/raw/River_kilometers/kilometrage-des-cours-deau.gpkg"
BUILDINGS_PATH = "data/raw/Buildings/ACT2023v2_Buildings3d_Footprints.gpkg"
COMMUNE_PATH = "data/raw/Population/LIMADM_COMMUNES.shp"
POPULATION_PATH = "data/raw/Population/rnpp_population_per_municipality.csv"
HEIGHT_GPKG_PATH = "data/raw/Population/LU00.gpkg"

def main():
    # Ensure outputs directory exists
    os.makedirs("outputs", exist_ok=True)
    
    print("Step 1: Generating AOI and filtering buildings...")
    aoi_gdf, filtered_buildings = generate_aoi_and_filter_buildings(
        config=config,
        data_path=KILOMETRAGE_PATH,
        # The 2023 register footprints are the single building source: the same
        # layer the digital twin displays, carrying buildingid, commune, lau2,
        # area and zmin.
        buildings_path=BUILDINGS_PATH,
        output_gpkg_path="outputs/alzette_aoi_buildings.gpkg"
    )
    
    print("Step 2: Processing heights, volumes, and population...")
    buildings_joined = calculate_population_and_heights(
        buildings_path="outputs/alzette_aoi_buildings.gpkg",
        commune_path=COMMUNE_PATH,
        population_path=POPULATION_PATH,
        height_gpkg_path=HEIGHT_GPKG_PATH,
        target_crs=config.TARGET_CRS
    )
    
    print("Step 2.5: Enriching buildings with OpenStreetMap industrial data...")
    # Apply directly to buildings_joined in-memory
    buildings_joined = enrich_with_osm_industrial_data(buildings_joined, aoi_gdf=aoi_gdf)
    #print("ALL COLUMNS NOW IN MEMORY:", buildings_joined.columns.tolist())
    
    print("Step 3 (Scenario 1): Applying simplified non-residential water and price model...")
    buildings_joined = non_residential_water_allocation(
        buildings_joined,
        total_water_volume=100000.0,
        percentage=0.10,
        unit_price=7.0
    )
    
    print("Step 3 (Scenario 1, 15-minute): Gauge-driven volume, tariff and fee per building...")
    # Window: GAUGE_START / GAUGE_END (ISO 8601, UTC), else the last 7 days.
    # GAUGE_READINGS_JSON points at saved readings, to run the pipeline without the API.
    tariff_series, fees_15min, _, gauge_areas = period_fees(
        buildings_joined,
        start=os.environ.get("GAUGE_START"),
        end=os.environ.get("GAUGE_END"),
        readings_json=os.environ.get("GAUGE_READINGS_JSON") or None,
        percentage=0.10,
        unit_price=7.0
    )
    # period_fees keeps the row order of buildings_joined: the fee columns attach
    # positionally, which also survives a duplicated index from the joins above.
    buildings_joined["water_fee_15min_mean_eur"] = fees_15min["mean_fee_eur"].to_numpy()
    buildings_joined["water_fee_15min_min_eur"] = fees_15min["min_fee_eur"].to_numpy()
    buildings_joined["water_fee_15min_max_eur"] = fees_15min["max_fee_eur"].to_numpy()
    buildings_joined["water_area_share_15min"] = fees_15min["area_share"].to_numpy()
    tariff_series.to_csv("outputs/timeseries_first_scenario_tariff.csv", index=False)
    fees_15min.to_csv("outputs/timeseries_first_scenario_building_fees.csv", index=False)
    print(" -> Section areas: " + ", ".join(f"{name} {area:,.0f} m2" for name, area in gauge_areas.items()))
    print(f" -> {len(tariff_series)} intervals, mean tariff {tariff_series['tariff_eur'].mean():,.2f} EUR")
    
    print("Step 3 (Scenario 2): Applying rainwater impact and imperviousness fee model...")
    buildings_joined = process_rainwater_impact(
        buildings_joined,
        total_rainwater_volume=120000.0,
        unit_price=4.5
    )
    
    print("Step 3 (Scenario 3): Applying flood risk proximity levy model...")
    buildings_joined = process_buildings_flood_risk(
        buildings_joined,
        river_path=KILOMETRAGE_PATH,
        distance_threshold=50.0,
        base_fee=150.0
    )
    
    print("Step 3 (Scenario 4): Applying urban runoff pollution and water quality surcharge...")
    buildings_joined = process_water_quality_charge(
        buildings_joined,
        quality_unit_price=2.5  # Unit price per m² adjusted for pollution factor
    )
    
    print("Step 3 (Scenario 5): Calculating building water consumption based on population...")
    buildings_joined = process_building_population_water_consumption(
        buildings_joined,
        liters_per_person=160.0,     # in liter
        unit_price_per_liter=0.005,  # 0.005 €/liter = 5.00 €/m³
        usable_space_pct=0.80        # 80% usable floor space efficiency ratio from literature
    )

    print("Step 4: Exporting final files...")
    export_results(
        buildings_joined,
        csv_path="outputs/buildings_population_final.csv",
        geojson_2d_path="outputs/alzette_non_residential_water_costs.geojson",
    )
    print("Pipeline completed successfully!")
    
if __name__ == "__main__":
    main()