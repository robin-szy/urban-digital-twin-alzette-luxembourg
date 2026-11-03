Data needed for this code:

kilometrage-des-cours-deau.gpkg: A GeoPackage file containing river kilometrage data used by the build_aoi()function to map the river stretch and define your Area of Interest (AOI).


BU.Building.gml: A Geography Markup Language (GML) file containing raw building polygon footprints that will be filtered to match your AOI.


LU00.gpkg: A GeoPackage file containing architectural details like external building heights (height), number of floors (floors), and building types (type).


LIMADM_COMMUNES.shp: A shapefile containing administrative boundaries for communes (municipalities) along with a matching identifier column (lau2, la_id, or equivalent).


rnpp_population_per_municipality.csv: A CSV file containing population statistics per municipality. It must include commune identification codes (lau2) and demographic columns such as femmes_mineures, hommes_mineurs, femmes_majeures, and hommes_majeures to calculate total population.
