import argparse
import gdal
import logging
import numpy as np
import os
import osr
import pkg_resources

from datetime import datetime
from multiply_core.observations import GeoTiffWriter, ObservationsFactory, is_valid, get_valid_files
from multiply_core.util import FileRef, Reprojection, get_time_from_string
from multiply_core.variables import Variable
from shapely.geometry import Polygon
from shapely.wkt import loads
from typing import List, Optional, Union

from multiply_post_processing.post_processor import EODataPostProcessor, PostProcessor, PostProcessorCreator, \
    PostProcessorType, VariablePostProcessor

__author__ = 'Tonio Fincke (Brockmann Consult GmbH)'

POST_PROCESSOR_CREATOR_REGISTRY = []
SINGLE_NAME_FORMAT = '{}_{}.tif'
DOUBLE_NAME_FORMAT = '{}_{}_{}.tif'

component_progress_logger = logging.getLogger('ComponentProgress')
component_progress_logger.setLevel(logging.INFO)
component_progress_formatter = logging.Formatter('%(levelname)s:%(name)s:%(message)s')
component_progress_logging_handler = logging.StreamHandler()
component_progress_logging_handler.setLevel(logging.INFO)
component_progress_logging_handler.setFormatter(component_progress_formatter)
component_progress_logger.addHandler(component_progress_logging_handler)

def add_post_processor_creator(post_processor_creator: PostProcessorCreator):
    POST_PROCESSOR_CREATOR_REGISTRY.append(post_processor_creator)


registered_post_processor_creators = pkg_resources.iter_entry_points('post_processor_creators')
for registered_post_processor_creator in registered_post_processor_creators:
    add_post_processor_creator(registered_post_processor_creator.load())


def get_post_processor_creators() -> List[PostProcessorCreator]:
    return POST_PROCESSOR_CREATOR_REGISTRY


def get_post_processors(requested_indicator_names: List[str]) -> List[PostProcessor]:
    """
    :param requested_indicator_names: Names of the indicators that shall be derived.
    :return: The post processors that can be used to derive the designated indicators.
    """
    post_processors = []
    for post_processor_creator in POST_PROCESSOR_CREATOR_REGISTRY:
        indicator_names = []
        indicator_descriptions = post_processor_creator.get_indicator_descriptions()
        for indicator_description in indicator_descriptions:
            if indicator_description.short_name in requested_indicator_names:
                indicator_names.append(indicator_description.short_name)
        if len(indicator_names) > 0:
            post_processors.append(post_processor_creator.create_post_processor(indicator_names))
    return post_processors


def get_post_processor_names() -> List[str]:
    """
    :return: the names of all post processors registered in the post processing component
    """
    post_processor_names = []
    for post_processor_creator in POST_PROCESSOR_CREATOR_REGISTRY:
        post_processor_names.append(post_processor_creator.get_name())
    return post_processor_names


def get_post_processor_description(name: str) -> str:
    """
    :param A name of a post-processor
    :return: the description of the post processor of the requested name
    """
    for post_processor_creator in POST_PROCESSOR_CREATOR_REGISTRY:
        if name == post_processor_creator.get_name():
            return post_processor_creator.get_description()
    raise ValueError('No post processor with name {} found.'.format(name))


def get_post_processor(name: str, indicator_names: List[str]) -> PostProcessor:
    """
    :param A name of a post-processor
    :return: the post processor of the requested name
    """
    for post_processor_creator in POST_PROCESSOR_CREATOR_REGISTRY:
        if name == post_processor_creator.get_name():
            return post_processor_creator.create_post_processor(indicator_names)
    raise ValueError('No post processor with name {} found.'.format(name))


def get_available_indicators() -> List[Variable]:
    """
    :return: the names of the indicators that can be derived using one of the registered post processors.
    """
    indicator_descriptions = []
    for post_processor_creator in POST_PROCESSOR_CREATOR_REGISTRY:
        post_processor_indicator_descriptions = post_processor_creator.get_indicator_descriptions()
        for indicator_description in post_processor_indicator_descriptions:
            if indicator_description not in indicator_descriptions:
                indicator_descriptions.append(indicator_description)
    return indicator_descriptions


# todo almost the same method is included in inference engine. Find way to harmonize
def _get_reprojection(spatial_resolution: int, roi: Union[str, Polygon], roi_grid: Optional[str] = None,
                      destination_grid: Optional[str] = None) -> Reprojection:
    if type(roi) is str:
        roi = loads(roi)
    roi_bounds = roi.bounds
    roi_center = roi.centroid
    roi_srs = _get_reference_system(roi_grid)
    destination_srs = _get_reference_system(destination_grid)
    wgs84_srs = _get_reference_system('EPSG:4326')
    if roi_srs is None:
        if destination_srs is None:
            roi_srs = wgs84_srs
            destination_srs = _get_projected_srs(roi_center)
        else:
            roi_srs = destination_srs
    elif destination_srs is None:
        if roi_srs.IsSame(wgs84_srs):
            destination_srs = _get_projected_srs(roi_center)
        else:
            raise ValueError('Cannot derive destination grid for roi grid {}. Please specify destination grid'.
                             format(roi_grid))
    return Reprojection(roi_bounds, spatial_resolution, spatial_resolution, destination_srs, roi_srs)


# todo the same method is included in inference engine. Find way to harmonize
def _get_projected_srs(roi_center):
    utm_zone = int(1 + (roi_center.coords[0][0] + 180.0) / 6.0)
    is_northern = int(roi_center.coords[0][1] > 0.0)
    spatial_reference_system = osr.SpatialReference()
    spatial_reference_system.SetWellKnownGeogCS('WGS84')
    spatial_reference_system.SetUTM(utm_zone, is_northern)
    return spatial_reference_system


# todo the same method is included in inference engine. Find way to harmonize
def _get_reference_system(wkt: str) -> Optional[osr.SpatialReference]:
    if wkt is None:
        return None
    spatial_reference = osr.SpatialReference()
    if wkt.startswith('EPSG:'):
        epsg_code = int(wkt.split(':')[1])
        spatial_reference.ImportFromEPSG(epsg_code)
    else:
        spatial_reference.ImportFromWkt(wkt)
    return spatial_reference


def _get_dummy_data_set():
    driver = gdal.GetDriverByName('MEM')
    dataset = driver.Create('', 360, 90, bands=1)
    dataset.SetGeoTransform((-180.0, 1.00, 0.0, 90.0, 0.0, -1.00))
    srs = osr.SpatialReference()
    srs.SetWellKnownGeogCS("WGS84")
    dataset.SetProjection(srs.ExportToWkt())
    dataset.GetRasterBand(1).WriteArray(np.ones((90, 360)))
    return dataset


def run_post_processing(indicator_names: List[str], data_path: str, output_path: str, roi: Union[str, Polygon],
                        spatial_resolution: int, variable_names: Optional[List[str]] = None,
                        roi_grid: Optional[str] = 'EPSG:4326', destination_grid: Optional[str] = None,
                        output_format: Optional[str] = 'GeoTiff'):
    post_processors = get_post_processors(indicator_names)
    for post_processor in post_processors:
        run_actual_post_processor(post_processor, data_path, output_path, roi, spatial_resolution, variable_names,
                                  roi_grid, destination_grid, output_format)


def run_post_processor(name: str, data_path: str, output_path: str, roi: Union[str, Polygon],
                       spatial_resolution: int, indicator_names: Optional[List[str]] = [],
                       variable_names: Optional[List[str]] = None, roi_grid: Optional[str] = 'EPSG:4326',
                       destination_grid: Optional[str] = None, output_format: Optional[str] = 'GeoTiff'):
    run_actual_post_processor(get_post_processor(name, indicator_names), data_path, output_path, roi,
                              spatial_resolution, variable_names, roi_grid, destination_grid, output_format)


# noinspection PyTypeChecker
def run_actual_post_processor(post_processor: PostProcessor, data_path: str, output_path: str,
                              roi: Union[str, Polygon], spatial_resolution: int,
                              variable_names: Optional[List[str]] = None, roi_grid: Optional[str] = 'EPSG:4326',
                              destination_grid: Optional[str] = None, output_format: Optional[str] = 'GeoTiff'):
    if post_processor.get_type() == PostProcessorType.EO_DATA_POST_PROCESSOR:
        _run_eo_data_post_processor(post_processor, data_path, output_path, roi, spatial_resolution, roi_grid,
                                    destination_grid, output_format)
    elif post_processor.get_type() == PostProcessorType.VARIABLE_POST_PROCESSOR:
        if variable_names is None:
            raise ValueError('No list with variable names be provided.')
        _run_variable_post_processor(post_processor, data_path, output_path, variable_names, roi, spatial_resolution,
                                     roi_grid, destination_grid, output_format)


def _run_eo_data_post_processor(post_processor: EODataPostProcessor, data_path: str, output_path: str,
                                roi: Union[str, Polygon], spatial_resolution: int, roi_grid: Optional[str],
                                destination_grid: Optional[str], output_format: Optional[str] = 'GeoTiff'):
    supported_eo_data_types = post_processor.get_names_of_supported_eo_data_types()
    file_refs = get_valid_files(data_path, supported_eo_data_types)
    reprojection = _get_reprojection(spatial_resolution, roi, roi_grid, destination_grid)
    observations_factory = ObservationsFactory()
    observations = observations_factory.create_observations(file_refs, reprojection)
    if observations.get_num_observations() < 2:
        logging.getLogger().info(f'Not enough observations found. '
                                 f'Can not conduct post processing for {post_processor.get_name()}')
        return
    for i in range(observations.get_num_observations() - 1):
        component_progress_logger.info(f'{int((i / (observations.get_num_observations() - 1)) * 100)}')
        start = observations.dates[i]
        end = observations.dates[i + 1]
        observations_subset = observations.get_observations_subset(start, end)
        indicator_dict = post_processor.process_observations(observations_subset)
        results = []
        file_names = []
        for indicator_name in indicator_dict:
            results.append(indicator_dict[indicator_name])
            file_names.append(os.path.join(output_path, DOUBLE_NAME_FORMAT.format(indicator_name,
                                                                              _format(start), _format(end))))
        _write(results, file_names, roi, spatial_resolution, roi_grid, destination_grid, output_format)


def _format(time: Union[datetime, str]):
    """
    Output: yyyymmdd
    """
    if type(time) == str:
        time = get_time_from_string(time)
    return time.strftime('%Y%m%d')


def _run_variable_post_processor(post_processor: VariablePostProcessor, data_path: str, output_path: str,
                                 variable_names: List[str], roi: Union[str, Polygon], spatial_resolution: int,
                                 roi_grid: Optional[str], destination_grid: Optional[str],
                                 output_format: Optional[str] = 'GeoTiff'):
    file_refs = get_valid_files(data_path, variable_names)
    file_ref_groups = _group_file_refs_by_date(file_refs)
    reprojection = _get_reprojection(spatial_resolution, roi, roi_grid, destination_grid)
    for i, date in enumerate(file_ref_groups):
        component_progress_logger.info(f'{int((i / (len(file_ref_groups.keys()))) * 100)}')
        data_files = {}
        file_refs_for_date = file_ref_groups[date]
        for variable_name in variable_names:
            for file_ref in file_refs_for_date:
                if is_valid(file_ref.url, variable_name):
                    data_files[variable_name] = file_ref.url
                    break
        variable_data = {}
        for variable_name in data_files:
            dataset = gdal.Open(data_files[variable_name])
            reprojected_data_set = reprojection.reproject(dataset)
            variable_data[variable_name] = reprojected_data_set.GetRasterBand(1).ReadAsArray()
        indicator_dict = post_processor.process_variables(variable_data)
        results = []
        file_names = []
        for indicator_name in indicator_dict:
            results.append(indicator_dict[indicator_name])
            file_names.append(os.path.join(output_path, SINGLE_NAME_FORMAT.format(indicator_name, _format(date))))
        _write(results, file_names, roi, spatial_resolution, roi_grid, destination_grid, output_format)


def _group_file_refs_by_date(file_refs: List[FileRef]) -> dict:
    # Note: This function relies on the assumption that for a variable, start and end time are equal and refer to a day
    file_ref_groups = {}
    for file_ref in file_refs:
        if file_ref.start_time not in file_ref_groups:
            file_ref_groups[file_ref.start_time] = []
        file_ref_groups[file_ref.start_time].append(file_ref)
    return file_ref_groups


def _write(indicators: List[np.array], file_names: List[str], roi: Union[str, Polygon], spatial_resolution: int,
           roi_grid: Optional[str], destination_grid: Optional[str], output_format: Optional[str] = 'GeoTiff'):
    reprojection = _get_reprojection(spatial_resolution, roi, roi_grid, destination_grid)
    if output_format == 'GeoTiff':
        reprojected_data_set = reprojection.reproject(_get_dummy_data_set())
        width = reprojected_data_set.RasterXSize
        height = reprojected_data_set.RasterYSize
        geo_transform = reprojected_data_set.GetGeoTransform()
        srs = reprojection.get_destination_srs()
        projection = srs.ExportToWkt()
        writer = GeoTiffWriter(file_names, geo_transform, projection, width, height, None, None)
        writer.write(indicators)
        writer.close()
    else:
        logging.warning('Writing of {} not supported. Can not write post-processing results.'.format(output_format))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='MULTIPLY Post Processing')
    parser.add_argument('-n', "--name", help='The name of the post-processor', required=True)
    parser.add_argument("-i", "--input_path", help="The directory where the input data is located.", required=True)
    parser.add_argument("-o", "--output_path", help="The output directory to which the output file shall be "
                                                    "written.", required=True)
    parser.add_argument("-f", "--format", help="The output format (default is GeoTiff).")
    parser.add_argument("-roi", "--roi", help="The region of interest describing the area to be retrieved. Not "
                                              "required if 'state_mask' is given.")
    parser.add_argument("-res", "--spatial_resolution", help="The spatial resolution of the destination grid. "
                                                             "Not required if 'state_mask' is given.")
    parser.add_argument("-rg", "--roi_grid", help="A representation of the spatial reference system in which the "
                                                  "roi is given, either as EPSG-code or as WKT representation. "
                                                  "If not given, it is assumed that the roi is given in the "
                                                  "destination spatial reference system.")
    parser.add_argument("-dg", "--destination_grid", help="A representation of the spatial reference system in which "
                                                          "the output shall be given, either as EPSG-code or as WKT "
                                                          "representation. If not given, the output is given in the "
                                                          "grid defined by the 'state_mask'.")
    args = parser.parse_args()
    if args.format is None:
        output_format = 'GeoTiff'
    else:
        output_format = args.format
    run_post_processor(name=args.name, data_path=args.input_path, output_path=args.output_path,
                       output_format=output_format, roi=args.roi, spatial_resolution=int(args.spatial_resolution),
                       roi_grid=args.roi_grid, destination_grid=args.destination_grid)
