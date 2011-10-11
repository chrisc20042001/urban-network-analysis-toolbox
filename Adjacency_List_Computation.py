# -------------------------------------------------------------------------------------
# Urban Network Analysis Toolbox for ArcGIS10
# Credits: Michael Mekonnen, Andres Sevtsuk
# MIT City Form Research Group
# Usage: Creative Commons Attribution - NonCommercial - ShareAlike 3.0 Unported License
# License: http://creativecommons.org/licenses/by-nc-sa/3.0/
# -------------------------------------------------------------------------------------

import arcpy
from Constants import *
from os import mkdir
from os.path import join
from Utils import *

arcpy.env.overwriteOutput = True # Enable overwriting

"""
|input_points|: point shape file marking entity (e.g. building) locations
|input_network|: street network in which |input_points| is located
|id_attribute|: the name of attribute that distinguishes between input points
|impedance_attribute|: distance between neighboring nodes will be based on this attribute
|accumulator_attributes|: distance between neighboring nodes will also be recorded for these attributes
|search_radius|: the maximum extent for centrality computation
|max_neighbor_separation|: the maximum extent for neighbor search
|output_location|: adjacency list dbf will be saved here
|adj_dbf_name|: the name of the adjacency list dbf
"""
def compute_adjacency_list(input_points,
                           input_network,
                           id_attribute,
                           impedance_attribute,
                           accumulator_attributes,
                           search_radius,
                           max_neighbor_separation,
                           output_location,
                           adj_dbf_name):

  # Number of points in |input_points|
  input_point_count = int(arcpy.GetCount_management(input_points).getOutput(0))

  # Make a directory to store all auxiliary files
  auxiliary_dir = join(output_location, AUXILIARY_DIR_NAME)
  if not arcpy.Exists(auxiliary_dir):
    mkdir(auxiliary_dir)

  # Record the edge and junction source names of |input_network|
  edge_feature = None
  junction_feature = None
  for source in arcpy.Describe(input_network).sources:
    if source.sourceType == EDGE_FEATURE:
      edge_feature = source.name
    elif source.sourceType in JUNCTION_FEATURE:
      junction_feature = source.name
  if edge_feature == None:
    arcpy.AddWarning(WARNING_NO_EDGE_FEATURE(input_network))
    raise Invalid_Input_Exception(input_network)
  if junction_feature == None:
    arcpy.AddWarning(WARNING_NO_JUNCTION_FEATURE(input_network))
    raise Invalid_Input_Exception(input_network)

  # Calculate network locations if not already calculated
  test_input_point = arcpy.UpdateCursor(input_points).next()
  locations_calculated = all(row_has_field(test_input_point, field) for field in NETWORK_LOCATION_FIELDS)
  if not locations_calculated:
    arcpy.AddMessage(CALCULATE_LOCATIONS_STARTED)
    arcpy.CalculateLocations_na(in_point_features=input_points,
                                in_network_dataset=input_network,
                                search_tolerance=SEARCH_TOLERANCE,
                                search_criteria="%s SHAPE; %s SHAPE;" % (junction_feature, edge_feature),
                                exclude_restricted_elements="INCLUDE")
    arcpy.AddMessage(CALCULATE_LOCATIONS_FINISHED)

  # Calculate barrier cost per input point if not already calculated
  barrier_costs_calculated = row_has_field(test_input_point, trim(BARRIER_COST_FIELD))
  if not barrier_costs_calculated:
    arcpy.AddMessage(BARRIER_COST_COMPUTATION_STARTED)
    # Add |BARRIER_COST_FIELD| column in |input_points|
    arcpy.AddField_management(in_table=input_points,
                              field_name=trim(BARRIER_COST_FIELD),
                              field_type="DOUBLE",
                              field_is_nullable="NON_NULLABLE")

    # Initialize a dictionary to store the frequencies of (SnapX, SnapY) values
    xy_count = {}
    # A method to retrieve a (SnapX, SnapY) pair for a row in |input_points|
    get_xy = lambda row: (row.getValue(trim("SnapX")), row.getValue(trim("SnapY")))

    barrier_pre_progress = Progress_Bar(input_point_count, 1, BARRIER_COST_PRE_PROCESSING)
    rows = arcpy.UpdateCursor(input_points)
    for row in rows:
      snap_xy = get_xy(row)
      if snap_xy in xy_count:
        xy_count[snap_xy] += 1
      else:
        xy_count[snap_xy] = 1
      barrier_pre_progress.step()

    # Populate |BARRIER_COST_FIELD|, this will be used in OD matrix computation
    barrier_progress = Progress_Bar(input_point_count, 1, BARRIER_COST_COMPUTATION)
    rows = arcpy.UpdateCursor(input_points)
    for row in rows:
      barrier_cost = BARRIER_COST / xy_count[get_xy(row)]
      row.setValue(trim(BARRIER_COST_FIELD), barrier_cost)
      rows.updateRow(row)
      barrier_progress.step()
    arcpy.AddMessage(BARRIER_COST_COMPUTATION_FINISHED)

  # Necessary files
  od_cost_matrix_layer = join(auxiliary_dir, OD_COST_MATRIX_LAYER_NAME)
  od_cost_matrix_lines = join(od_cost_matrix_layer, OD_COST_MATRIX_LINES)
  temp_adj_dbf_name = "%s~.dbf" % adj_dbf_name[:-4]
  temp_adj_dbf = join(output_location, temp_adj_dbf_name)
  adj_dbf = join(output_location, adj_dbf_name)
  partial_adj_dbf = join(auxiliary_dir, PARTIAL_ADJACENCY_LIST_NAME)
  polygons = join(auxiliary_dir, POLYGONS_SHAPEFILE_NAME)
  raster = join(auxiliary_dir, RASTER_NAME)
  polygons_layer = join(auxiliary_dir, POLYGONS_LAYER_NAME)
  input_points_layer = join(auxiliary_dir, INPUT_POINTS_LAYER_NAME)

  # Make sure none of these files already exists
  for file in [od_cost_matrix_layer,
               temp_adj_dbf,
               adj_dbf,
               partial_adj_dbf,
               polygons,
               raster,
               polygons_layer,
               input_points_layer,
               od_cost_matrix_lines]:
    if arcpy.Exists(file):
      arcpy.Delete_management(file)

  # Cutoff radius for OD matrix computation
  cutoff_radius = 2 * BARRIER_COST + min(search_radius, BARRIER_COST / 2)

  # Compute OD matrix
  arcpy.MakeODCostMatrixLayer_na(in_network_dataset=input_network,
                                 out_network_analysis_layer=od_cost_matrix_layer,
                                 impedance_attribute=impedance_attribute,
                                 default_cutoff=str(cutoff_radius),
                                 accumulate_attribute_name=accumulator_attributes,
                                 UTurn_policy="ALLOW_UTURNS",
                                 hierarchy="NO_HIERARCHY",
                                 output_path_shape="NO_LINES")

  # Determine raster cell size
  input_points_extent = arcpy.Describe(input_points).Extent
  raster_cell_count = max(1, input_point_count / POINTS_PER_RASTER_CELL)
  raster_cell_area = input_points_extent.width * input_points_extent.height / raster_cell_count
  raster_cell_size = int(sqrt(raster_cell_area))

  # Construct |raster| from |input_points|
  arcpy.PointToRaster_conversion(in_features=input_points,
                                 value_field=id_attribute,
                                 out_rasterdataset=raster,
                                 cell_assignment="MOST_FREQUENT",
                                 priority_field="NONE",
                                 cellsize=str(raster_cell_size))

  # Construct |polygons| from |raster|
  arcpy.RasterToPolygon_conversion(in_raster=raster,
                                   out_polygon_features=polygons,
                                   simplify="NO_SIMPLIFY",
                                   raster_field="VALUE")

  # Export empty |od_cost_matrix_lines| to |temp_dbf| to start an empty adjacency list
  arcpy.TableToTable_conversion(in_rows=od_cost_matrix_lines,
                                out_path=output_location,
                                out_name=temp_adj_dbf_name)

  # Construct |polygons_layer| and |input_points_layer|
  for (feature, layer) in [(polygons, polygons_layer),
                           (input_points, input_points_layer)]:
    arcpy.MakeFeatureLayer_management(in_features=feature,
                                      out_layer=layer)

  # Compute adjacency list, one raster cell at a time
  progress = Progress_Bar(raster_cell_count, 1, STEP_1)
  rows = arcpy.UpdateCursor(polygons)
  for row in rows:
    # Select the current polygon
    arcpy.SelectLayerByAttribute_management(in_layer_or_view=polygons_layer,
                                            selection_type="NEW_SELECTION",
                                            where_clause="FID = %s" % str(row.FID))

    """
    |sub_layer|: one of "Origins", "Destinations", "Barrier Points"
    |field_mappings|: field mappings in addition to those for "Name" and "CurbApproach"
    """
    def add_locations(sub_layer, field_mappings=""):
      arcpy.AddLocations_na(in_network_analysis_layer=od_cost_matrix_layer,
                            sub_layer=sub_layer,
                            in_table=input_points_layer,
                            field_mappings="Name %s #; CurbApproach # 0; %s" % (id_attribute, field_mappings),
                            search_tolerance=SEARCH_TOLERANCE,
                            search_criteria="%s SHAPE; %s SHAPE;" % (junction_feature, edge_feature),
                            append="CLEAR",
                            snap_to_position_along_network="SNAP",
                            snap_offset=SNAP_OFFSET)

    # Origins
    arcpy.SelectLayerByLocation_management(in_layer=input_points_layer,
                                           select_features=polygons_layer)
    add_locations("Origins")

    # Destinations
    arcpy.SelectLayerByLocation_management(in_layer=input_points_layer,
                                           select_features=polygons_layer,
                                           search_distance=max_neighbor_separation)
    add_locations("Destinations")

    # Point barriers
    add_locations("Point Barriers", ("FullEdge # 0;"
                                     "BarrierType # 2;"
                                     "Attr_%s %s #;" % (impedance_attribute, trim(BARRIER_COST_FIELD))))

    # Solve OD Cost matrix
    arcpy.Solve_na(in_network_analysis_layer=od_cost_matrix_layer,
                   ignore_invalids="SKIP")

    # Add origin and destination fields to the adjacency list dbf
    for (index, field) in [(0, ORIGIN_ID_FIELD_NAME),
                           (1, DESTINATION_ID_FIELD_NAME)]:
      arcpy.CalculateField_management(in_table=od_cost_matrix_lines,
                                      field=field,
                                      expression="!Name!.split(' - ')[%d]" % index,
                                      expression_type="PYTHON")

    # Record actual distance between neighboring nodes
    distance_field = trim("Total_%s" % impedance_attribute)
    arcpy.CalculateField_management(in_table=od_cost_matrix_lines,
                                    field=distance_field,
                                    expression="!%s! - 2 * %d" % (distance_field, BARRIER_COST),
                                    expression_type="PYTHON")

    # Append result to |temp_adj_dbf|
    arcpy.TableToTable_conversion(in_rows=od_cost_matrix_lines,
                                  out_path=auxiliary_dir,
                                  out_name=PARTIAL_ADJACENCY_LIST_NAME)
    arcpy.Append_management(inputs=partial_adj_dbf,
                            target=temp_adj_dbf,
                            schema_type="TEST")

    progress.step()

  # Copy data from |temp_adj_dbf| to |adj_dbf|
  arcpy.Rename_management(in_data=temp_adj_dbf,
                          out_data=adj_dbf)

  # Clean up
  for file in [od_cost_matrix_layer,
               partial_adj_dbf,
               polygons,
               raster,
               polygons_layer,
               input_points_layer,
               auxiliary_dir]:
    if arcpy.Exists(file):
      arcpy.Delete_management(file)