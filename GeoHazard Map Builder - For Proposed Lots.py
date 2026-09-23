import sys
import os
import traceback
import processing
from qgis.core import (
    QgsProject,
    QgsCategorizedSymbolRenderer,
    QgsRendererCategory,
    QgsFillSymbol,
    QgsProcessing,
    QgsHillshadeRenderer
)
from qgis.PyQt.QtGui import QPainter

# ==============================================================================
# PROJECT CONFIGURATION & PARAMETERS (EDIT HERE)
# ==============================================================================
# 1. LAYER NAMES IN QGIS TOC
DEM_LAYER_NAME      = "output_be"
BOUNDARY_LAYER_NAME = "Proposed Lots"
OUTPUT_LAYER_NAME   = "Geohazard Zones"

# 2. ANALYSIS PARAMETERS
SLOPE_THRESHOLD     = 16.7   # Threshold angle in degrees (e.g. 17.1 or 27.0)
SIEVE_PIXELS        = 50     # Pixel threshold to remove isolated noise
MIN_HOLE_AREA_M2    = 50     # Minimum area (sq m) for interior polygon holes
AREA_CONVERSION_DIV = 10000  # Divide m² by this factor for final units (10000 = Hectares)
AREA_UNIT_LABEL     = "area_ha"

# 3. FILE EXPORT SETTINGS
GPKG_FILENAME       = "Geohazard_Zones.gpkg"
CSV_FILENAME        = "Slope_Hazard_Zone_Summary.csv"

# 4. SYMBOLOGY & VISUALS
ZONE1_LABEL         = f"Zone 1 (< {SLOPE_THRESHOLD}°)"
ZONE1_FILL          = '#00E676'  # High-Vis Electric Green
ZONE1_OUTLINE       = '#004D40'  # Deep Forest Outline

ZONE2_LABEL         = f"Zone 2 (>= {SLOPE_THRESHOLD}°)"
ZONE2_FILL          = '#FF1744'  # High-Vis Vivid Red
ZONE2_OUTLINE       = '#880E4F'  # Deep Wine Outline

OUTLINE_WIDTH_MM    = '0.3'      # Stroke width in mm
RASTER_AZIMUTH      = 315.0      # Hillshade light source azimuth
RASTER_ALTITUDE     = 35.0       # Hillshade light source altitude

# ==============================================================================
# CORE PROCESSING ENGINE
# ==============================================================================
def run_geohazard_pipeline():
    field_name = "slope_class"
    slope_ranges = [-999.0, SLOPE_THRESHOLD, 1, SLOPE_THRESHOLD, 900.0, 2]

    # Resolve Dynamic Path to Project Home
    project_folder = QgsProject.instance().homePath() or os.path.dirname(QgsProject.instance().fileName()) or os.getcwd()
    save_filepath = os.path.join(project_folder, GPKG_FILENAME)

    try:
        def fetch_layer(name):
            layers = QgsProject.instance().mapLayersByName(name)
            if not layers:
                available = [l.name() for l in QgsProject.instance().mapLayers().values()]
                raise ValueError(
                    f"Layer '{name}' was not found in your QGIS Layers panel.\n"
                    f"Layers currently in your project: {available}"
                )
            return layers[0]

        print("--> Fetching input layers from project...")
        dem_layer = fetch_layer(DEM_LAYER_NAME)
        boundary_layer = fetch_layer(BOUNDARY_LAYER_NAME)

        print("--> Calculating slope raster directly from DEM...")
        slope_layer = processing.run("qgis:slope", {
            'INPUT': dem_layer,
            'Z_FACTOR': 1.0,
            'OUTPUT': QgsProcessing.TEMPORARY_OUTPUT
        })['OUTPUT']

        print("--> Fixing boundary layer geometries...")
        boundary_valid = processing.run("native:fixgeometries", {
            'INPUT': boundary_layer,
            'OUTPUT': 'memory:'
        })['OUTPUT']

        # 1. RECLASSIFY & SIEVE
        print(f"--> Reclassifying slope raster at {SLOPE_THRESHOLD}° threshold...")
        reclassed_raster = processing.run("native:reclassifybytable", {
            'INPUT_RASTER': slope_layer,
            'RASTER_BAND': 1,
            'TABLE': slope_ranges,
            'NO_DATA': 1,
            'RANGE_BOUNDARIES': 1,
            'NODATA_FOR_MISSING': False,
            'DATA_TYPE': 5,
            'OUTPUT': QgsProcessing.TEMPORARY_OUTPUT
        })['OUTPUT']

        print(f"--> Sieving raster noise ({SIEVE_PIXELS} px threshold)...")
        homogenized_raster = processing.run("gdal:sieve", {
            'INPUT': reclassed_raster,
            'THRESHOLD': SIEVE_PIXELS,
            'EIGHT_CONNECTEDNESS': True,
            'NO_MASK': True,
            'OUTPUT': QgsProcessing.TEMPORARY_OUTPUT
        })['OUTPUT']

        print("--> Polygonizing sieved raster...")
        raw_polygons = processing.run("gdal:polygonize", {
            'INPUT': homogenized_raster,
            'BAND': 1,
            'FIELD': field_name,
            'EIGHT_CONNECTEDNESS': True,
            'OUTPUT': QgsProcessing.TEMPORARY_OUTPUT
        })['OUTPUT']

        fixed_raw = processing.run("native:fixgeometries", {
            'INPUT': raw_polygons,
            'OUTPUT': 'memory:'
        })['OUTPUT']

        # 2. ISOLATE ZONES
        print("--> Processing Zone 2 (> threshold) geometries...")
        zone2_raw = processing.run("native:extractbyexpression", {
            'INPUT': fixed_raw,
            'EXPRESSION': f'to_int("{field_name}") = 2',
            'OUTPUT': 'memory:'
        })['OUTPUT']

        zone2_dissolved = processing.run("native:dissolve", {
            'INPUT': zone2_raw,
            'SEPARATE_DISJOINT': True,
            'OUTPUT': 'memory:'
        })['OUTPUT']

        zone2_solid = processing.run("native:deleteholes", {
            'INPUT': zone2_dissolved,
            'MIN_AREA': MIN_HOLE_AREA_M2,
            'OUTPUT': 'memory:'
        })['OUTPUT']

        zone2_clipped = processing.run("native:clip", {
            'INPUT': zone2_solid,
            'OVERLAY': boundary_valid,
            'OUTPUT': 'memory:'
        })['OUTPUT']

        zone2_final = processing.run("native:fieldcalculator", {
            'INPUT': zone2_clipped,
            'FIELD_NAME': field_name,
            'FIELD_TYPE': 1,
            'FORMULA': '2',
            'OUTPUT': 'memory:'
        })['OUTPUT']

        print("--> Processing Zone 1 (<= threshold) geometries...")
        zone1_geometry = processing.run("native:difference", {
            'INPUT': boundary_valid,
            'OVERLAY': zone2_final,
            'OUTPUT': 'memory:'
        })['OUTPUT']

        zone1_final = processing.run("native:fieldcalculator", {
            'INPUT': zone1_geometry,
            'FIELD_NAME': field_name,
            'FIELD_TYPE': 1,
            'FORMULA': '1',
            'OUTPUT': 'memory:'
        })['OUTPUT']

        combined_zones = processing.run("native:mergevectorlayers", {
            'LAYERS': [zone1_final, zone2_final],
            'OUTPUT': 'memory:'
        })['OUTPUT']

        final_valid = processing.run("native:fixgeometries", {
            'INPUT': combined_zones,
            'OUTPUT': 'memory:'
        })['OUTPUT']

        # 3. STATS & AREA CALCULATIONS
        print("--> Calculating elevation zonal stats...")
        zonal_dem = processing.run("native:zonalstatisticsfb", {
            'INPUT': final_valid,
            'INPUT_RASTER': dem_layer,
            'RASTER_BAND': 1,
            'COLUMN_PREFIX': 'elev_',
            'STATISTICS': [2, 5, 6],
            'OUTPUT': 'memory:'
        })['OUTPUT']

        print("--> Calculating slope zonal stats...")
        zonal_slope = processing.run("native:zonalstatisticsfb", {
            'INPUT': zonal_dem,
            'INPUT_RASTER': slope_layer,
            'RASTER_BAND': 1,
            'COLUMN_PREFIX': 'slope_',
            'STATISTICS': [2, 6],
            'OUTPUT': 'memory:'
        })['OUTPUT']

        calc_m2 = processing.run("native:fieldcalculator", {
            'INPUT': zonal_slope,
            'FIELD_NAME': 'area_m2',
            'FIELD_TYPE': 0,
            'FIELD_LENGTH': 12,
            'FIELD_PRECISION': 2,
            'FORMULA': '$area',
            'OUTPUT': 'memory:'
        })['OUTPUT']

        calc_area = processing.run("native:fieldcalculator", {
            'INPUT': calc_m2,
            'FIELD_NAME': AREA_UNIT_LABEL,
            'FIELD_TYPE': 0,
            'FIELD_LENGTH': 10,
            'FIELD_PRECISION': 4,
            'FORMULA': f'$area / {AREA_CONVERSION_DIV}',
            'OUTPUT': 'memory:'
        })['OUTPUT']

        # DROP FID FIELD TO PREVENT UNIQUE CONSTRAINT CONFLICTS ON EXPORT
        final_polygons = processing.run("native:deletecolumn", {
            'INPUT': calc_area,
            'COLUMN': ['fid', 'FID'],
            'OUTPUT': f'memory:{OUTPUT_LAYER_NAME}'
        })['OUTPUT']

        # 4. SAVE TO DISK
        print(f"--> Exporting vector features to Project Home:\n    {save_filepath}")
        os.makedirs(os.path.dirname(save_filepath), exist_ok=True)
        
        # Remove existing file if present to clear OGR locks
        if os.path.exists(save_filepath):
            try:
                os.remove(save_filepath)
            except Exception:
                pass

        processing.run("native:savefeatures", {
            'INPUT': final_polygons,
            'OUTPUT': save_filepath
        })
        print("--> File successfully saved to disk.")

        # 5. SYMBOLOGY & CANVAS RENDER
        print("--> Applying custom categorized symbology...")
        sym_zone1 = QgsFillSymbol.createSimple({
            'color': ZONE1_FILL,
            'outline_color': ZONE1_OUTLINE,
            'outline_width': OUTLINE_WIDTH_MM,
            'outline_width_unit': 'MM'
        })
        sym_zone2 = QgsFillSymbol.createSimple({
            'color': ZONE2_FILL,
            'outline_color': ZONE2_OUTLINE,
            'outline_width': OUTLINE_WIDTH_MM,
            'outline_width_unit': 'MM'
        })

        categories = [
            QgsRendererCategory(1, sym_zone1, ZONE1_LABEL),
            QgsRendererCategory(2, sym_zone2, ZONE2_LABEL)
        ]
        renderer = QgsCategorizedSymbolRenderer(field_name, categories)
        final_polygons.setRenderer(renderer)

        final_polygons.setBlendMode(QPainter.CompositionMode_Multiply)
        final_polygons.setOpacity(1.0)

        # Build Multidirectional Hillshade beneath vectors
        multi_hs_name = f"{DEM_LAYER_NAME}_Hillshade"
        for old_lyr in QgsProject.instance().mapLayersByName(multi_hs_name):
            QgsProject.instance().removeMapLayer(old_lyr)

        multi_hs = dem_layer.clone()
        multi_hs.setName(multi_hs_name)
        multi_renderer = QgsHillshadeRenderer(multi_hs.dataProvider(), 1, RASTER_AZIMUTH, RASTER_ALTITUDE)
        multi_renderer.setMultiDirectional(True)
        multi_hs.setRenderer(multi_renderer)

        # Clear existing output layer if present
        for old_lyr in QgsProject.instance().mapLayersByName(OUTPUT_LAYER_NAME):
            QgsProject.instance().removeMapLayer(old_lyr)

        # Push to QGIS map canvas
        QgsProject.instance().addMapLayer(multi_hs, True)
        QgsProject.instance().addMapLayer(final_polygons, True)

        from qgis.utils import iface
        if iface:
            iface.mapCanvas().refresh()

        print(f"\n SUCCESS! Stack generated in project home: {project_folder}")

    except Exception as e:
        print("\n!!! PIPELINE FAILURE !!!")
        print(f"Error Message: {e}")
        traceback.print_exc(file=sys.stdout)


# Execute Pipeline
run_geohazard_pipeline()