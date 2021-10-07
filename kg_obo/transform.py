import tempfile
import kgx.cli  # type: ignore
from kgx.config import get_logger # type: ignore
from tqdm import tqdm  # type: ignore
import yaml  # type: ignore
import requests  # type: ignore
from datetime import datetime
from io import StringIO
import boto3  # type: ignore

import os
import shutil
import logging
import mmap
import re
import sys

from xml.sax._exceptions import SAXParseException  # type: ignore
from rdflib.exceptions import ParserError # type: ignore

import kg_obo.obolibrary_utils
import kg_obo.upload
from urllib.parse import quote


def delete_path(root_dir: str, omit: list = []) -> bool:
    """ Deletes a path recursively, i.e., everything in
    the provided directory and all its subdirectories.
    :param root_dir: str of the path to begin with
    :param omit: list of path(s) to keep, i.e., don't delete them
    :return: bool, True if successful
    """
    success = True

    try:
        for filename in os.listdir(root_dir):
            file_path = os.path.join(root_dir, filename)
            if filename not in omit:
                if os.path.isfile(file_path) or os.path.islink(file_path):
                    os.unlink(file_path)
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path)
    except IOError as e:
        print(f"Error in deleting {root_dir}: {e}")
        success = False

    return success

def retrieve_obofoundry_yaml(
        yaml_url: str = 'https://raw.githubusercontent.com/OBOFoundry/OBOFoundry.github.io/master/registry/ontologies.yml',
        skip: list = [],
        get_only: list = []) -> list:
    """ Retrieve YAML containing list of all ontologies in OBOFoundry
    :param yaml_url: a stable URL containing a YAML file that describes all the OBO ontologies:
    :param skip: which ontologies should we skip
    :return: parsed yaml describing ontologies to transform
    """
    yaml_req = requests.get(yaml_url)
    yaml_content = yaml_req.content.decode('utf-8')
    yaml_parsed = yaml.safe_load(yaml_content)
    if not yaml_parsed or 'ontologies' not in yaml_parsed:
        raise RuntimeError(f"Can't retrieve ontology info from YAML at this url {yaml_url}")
    else:
        yaml_onto_list: list = yaml_parsed['ontologies']

    if len(skip) > 0:
        yaml_onto_list_filtered = \
            [ontology for ontology in yaml_onto_list if ontology['id'] not in skip \
            if ("is_obsolete" not in ontology) or (ontology['is_obsolete'] == False)
            ]
    elif len(get_only) > 0:
        yaml_onto_list_filtered = \
            [ontology for ontology in yaml_onto_list if ontology['id'] in get_only \
            if ("is_obsolete" not in ontology) or (ontology['is_obsolete'] == False)
            ]
    else:
        yaml_onto_list_filtered = \
            [ontology for ontology in yaml_onto_list \
            if ("is_obsolete" not in ontology) or (ontology['is_obsolete'] == False)
            ]

    return yaml_onto_list_filtered


def kgx_transform(input_file: list, input_format: str,
                  output_file: str, output_format: str, logger: object) -> tuple:
    """Call KGX transform and report success status (bool)

    :param input_file: list of files to transform
    :param input_format: input format
    :param output_file: output file root
    :param output_format: output format
    :param logger: logger
    :return: tuple - (bool for did transform work?, bool for any errors encountered)
    """
    success = True
    errors = False

    bnode_errors = "BNode Errors"
    other_errors = "Other Errors"
    output_msg = f"No errors in transformation of {input_file} to {output_format}"
    
    log_file_name = f"{output_format}_transform.log"
    log_file_path = os.path.join(os.path.dirname(output_file),log_file_name)

    # We stream the KGX logs to their own output to capture them
    # and also set up log output to a file which will accompany the transformed output
    log_stream = StringIO()
    log_handler = logging.StreamHandler(log_stream)
    log_file_handler = logging.FileHandler(log_file_path)
    log_handler.setLevel(logging.WARNING)
    log_file_handler.setLevel(logging.INFO)
    # Logger doesn't know it's already an instance, so it throws an error
    try:
        logger.addHandler(hdlr=log_handler)  # type: ignore
    except TypeError:
        pass
    try:
        logger.addHandler(hdlr=log_file_handler)  # type: ignore
    except TypeError:
        pass
 
    try:
        if output_format == "tsv":
            kgx.cli.transform(inputs=input_file,
                          input_format=input_format,
                          output=output_file,
                          output_format=output_format,
                          output_compression="tar.gz")
        else:
            kgx.cli.transform(inputs=input_file,
                          input_format=input_format,
                          output=f"{output_file}.{output_format}",
                          output_format=output_format)
        
        # Need to parse the log output to aggregate it
        error_collect = {bnode_errors:0, other_errors:0}

        for line in log_stream.getvalue().splitlines():
            if line[0:31] == "Do not know how to handle BNode":
                error_collect[bnode_errors] = error_collect[bnode_errors] + 1
            else:
                error_collect[other_errors] = error_collect[other_errors] + 1

        if sum(error_collect.values()) > 0:  # type: ignore
            output_msg = f"Encountered errors in transforming or parsing to {output_format}: {error_collect}"
            errors = True

    except (FileNotFoundError,
            SAXParseException,
            ParserError,
            Exception,
            TypeError) as e:
        success = False
        output_msg = f"KGX problem while transforming {input_file} to {output_format} due to {e}"

    log_handler.flush()
    log_file_handler.flush()

    return (success, errors, output_msg)

def get_owl_iri(input_file_name: str) -> tuple:
    """
    Extracts version IRI from OWL definitions.
    Here, the IRI is the full URL of the origin OWL,
    as naming conventions vary.
    Avoids much file parsing as the IRI should be near the top of the file.
    Does some string parsing to get a shorter version number.
    Versions may take multiple formats across OBOs.
    If an IRI is not provided (i.e., the OWL does not contain owl:versionIRI
    in its header metadata) then we try the value of oboInOwl:date instead.
    The date value, if present, is used as a replacement version identifier,
    not a replacement IRI.
    The rdf:about value is also checked - this may not contain a version,
    but it may contain a URL we can use as an IRI.
    :param input_file_name: name of OWL format file to extract IRI from
    :return: tuple of (str of IRI, str of version)
    """
 
    # Most IRIs take this format - there are some exceptions
    iri_tag = b'owl:versionIRI rdf:resource=\"(.*)\"'
    iri_about_tag = b'owl:Ontology rdf:about=\"(.*)\"'
    date_tag = b'oboInOwl:date rdf:datatype=\"http://www.w3.org/2001/XMLSchema#string\">([^\<]+)'
    date_dc_tag = b'dc:date xml:lang=\"en\">([^\<]+)'
    version_info_tag = b'owl:versionInfo rdf:datatype=\"http://www.w3.org/2001/XMLSchema#string\">([^\<]+)'
    short_version_info_tag = b'<owl:versionInfo>([^\<]+)'

    #The default IRI/version - only used if values aren't provided.
    iri = "release"
    version = "release"

    try:
        with open(input_file_name, 'rb', 0) as owl_file, \
            mmap.mmap(owl_file.fileno(), 0, access=mmap.ACCESS_READ) as owl_string:
            iri_search = re.search(iri_tag, owl_string)  # type: ignore
            iri_about_tag_search = re.search(iri_about_tag, owl_string) # type: ignore
            date_search = re.search(date_tag, owl_string)  # type: ignore
            date_dc_search = re.search(date_dc_tag, owl_string)  # type: ignore
            version_info_search = re.search(version_info_tag, owl_string)  # type: ignore
            short_version_info_search = re.search(short_version_info_tag, owl_string)  # type: ignore
            # mypy doesn't like re and mmap objects
            if iri_search:
                iri = (iri_search.group(1)).decode("utf-8")
                try:
                    raw_version = (iri.split("/"))[-2]
                    if raw_version == "fao":
                        version = quote((iri.split("/"))[-3])
                    else:
                        version = quote(raw_version)
                except IndexError:
                    pass
            elif iri_about_tag_search: #In this case, we likely don't have a version
                iri = (iri_about_tag_search.group(1)).decode("utf-8")
            else:
                print("Version IRI not found.")
            
            # If we didn't get a version out of the IRI, look elsewhere
            for search_type in [date_search, date_dc_search, 
                                version_info_search, short_version_info_search]:
                if search_type and version == "release":
                    version_info = (search_type.group(1)).decode("utf-8")
                    version = quote(version_info)
            if version == "release":
                print("Neither versioned IRI or release date found.")
    except ValueError: #Should not happen unless OWL definitions are missing/broken
        print("Could not parse OWL definitions enough to locate version IRI or release date.")

    return (iri, version)

def track_obo_version(name: str = "", iri: str = "",
                      version: str = "", bucket: str = "",
                      track_file_local_path: str = "data/tracking.yaml",
                      track_file_remote_path: str = "kg-obo/tracking.yaml"
                      ) -> None:
    """
    Writes OBO version as per IRI to tracking.yaml.
    Note this tracking file is on the root of the S3 kg-obo directory.
    :param name: name of OBO, as OBO ID
    :param iri: full OBO VersionIRI, as URL
    :param version: short OBO version
    :param track_file_local_path: where to look for local tracking.yaml file
    :param track_file_remote_path: where to look for remote tracking.yaml file
    """

    client = boto3.client('s3')

    client.download_file(Bucket=bucket, Key=track_file_remote_path, Filename=track_file_local_path)

    with open(track_file_local_path, 'r') as track_file:
        tracking = yaml.load(track_file, Loader=yaml.BaseLoader)

    #If we already have a version, move it to archive
    if tracking["ontologies"][name]["current_version"] != "NA":
        if "archive" not in tracking["ontologies"][name]:
            tracking["ontologies"][name]["archive"] = []
        tracking["ontologies"][name]["archive"].append({"iri": iri, "version": version})
    else:
        tracking["ontologies"][name]["current_iri"] = iri
        tracking["ontologies"][name]["current_version"] = version

    with open(track_file_local_path, 'w') as track_file:
        track_file.write(yaml.dump(tracking))

    client.upload_file(Filename=track_file_local_path, Bucket=bucket, Key=track_file_remote_path,
                        ExtraArgs={'ACL':'public-read'})

def transformed_obo_exists(name: str, iri: str, s3_test=False, bucket: str = "",
                           tracking_file_local_path: str = "data/tracking.yaml",
                           tracking_file_remote_path: str = "kg-obo/tracking.yaml"
                           ) -> bool:
    """
    Read tracking.yaml to determine if transformed version of this OBO exists.

    :param name: string of short OBO name, e.g., bfo
    :param iri: iri of OBO version
    :return: boolean, True if this OBO and version already exist as transformed
    """
    
    exists = False # Assume we don't have the OBO yet unless proven otherwise
    
    #If testing, assume OBO transform does not exist as we aren't really reading tracking
    if s3_test:
        return exists
    
    client = boto3.client('s3')

    client.download_file(bucket, tracking_file_remote_path, tracking_file_local_path)

    with open(tracking_file_local_path, 'r') as track_file:
        tracking = yaml.load(track_file, Loader=yaml.BaseLoader)

    # Check current and previous versions
    if tracking["ontologies"][name]["current_iri"] == iri:
        exists = True
    elif "archive" in tracking["ontologies"][name] and \
        iri in [pair["iri"] for pair in tracking["ontologies"][name]["archive"]]:
            exists = True

    return exists

def download_ontology(url: str, file: str, logger: object, no_dl_progress: bool,
                      header_only: bool) -> bool:
    """
    Download ontology from URL

    :param url: url to download from
    :param file: file to download into
    :param logger:
    :param no_dl_progress: bool, if True then download progress bar is suppressed
    :param header_only: bool, if True then only download enough of file to check IRI/version
    :return: boolean indicating whether download worked
    """
    try:
        req = requests.get(url, stream=True)
        file_size = int(req.headers['Content-Length'])
        chunk_size = 2048
        with open(file, 'wb') as outfile:
            if not no_dl_progress:
                if not header_only:
                    pbar = tqdm(unit="B", total=file_size, unit_scale=True,
                        unit_divisor=chunk_size)
                else:
                    pbar = tqdm(unit="B", total=chunk_size, unit_scale=True,
                        unit_divisor=chunk_size)
            for chunk in req.iter_content(chunk_size=chunk_size):
                if chunk:
                    if not no_dl_progress:
                        pbar.update(len(chunk))
                    outfile.write(chunk)
                if header_only:
                    break
        return True
    except (KeyError, requests.exceptions.RequestException) as e:
        logger.error(e)  # type: ignore
        return False

def imports_requested(input_file_name: str) -> list:
    """
    Given an OWL file, searches for and returns list of import statements.
    :param file: file to parse
    :return: list of strings, each the name of an import, e.g. "upheno/metazoa.owl"
    """

    imports = []
    import_tag = b'owl:imports rdf:resource=\"(.*)\"'

    try:
        with open(input_file_name, 'rb', 0) as owl_file, \
            mmap.mmap(owl_file.fileno(), 0, access=mmap.ACCESS_READ) as owl_string:
            import_search = re.findall(import_tag, owl_string)  # type: ignore
            # mypy doesn't like re and mmap objects
            if len(import_search) > 0:
                for match in import_search:
                    imports.append(match.decode("utf-8"))
            else:
                print("No imports found.")
    except ValueError: #Should not happen unless OWL definitions are missing/broken
        print("Could not parse OWL definitions enough to locate any imports.")

    return imports

def run_transform(skip: list = [], get_only: list = [], bucket="bucket",
                  save_local=False, s3_test=False,
                  no_dl_progress=False,
                  force_index_refresh=False,
                  lock_file_remote_path: str = "kg-obo/lock",
                  log_dir="logs", data_dir="data",
                  remote_path="kg-obo",
                  track_file_local_path: str = "data/tracking.yaml",
                  tracking_file_remote_path: str = "kg-obo/tracking.yaml"
                  ) -> bool:
    """
    Perform setup, then kgx-mediated transforms for all specified OBOs.
    :param skip: list of OBOs to skip, by ID
    :param get_only: list of OBOs to transform, by ID (otherwise do all)
    :param bucket: str of S3 bucket, to be specified as argument
    :param save_local: bool for whether to retain transform results on local disk
    :param s3_test: bool for whether to perform mock S3 upload only
    :param no_dl_progress: bool for whether to hide download progress bars
    :param lock_file_remote_path: str of path for lock file on S3
    :param log_dir: str of local dir where any logs should be saved
    :param data_dir: str of local dir where data should be saved
    :param remote_path: str of remote path on S3 bucket
    :param track_file_local_path: str of local path for tracking file
    :param tracking_file_remote_path: str of path of tracking file on S3
    :return: boolean indicating success or existing run encountered (False for unresolved error)
    """

    # Set up logging
    timestring = (datetime.now()).strftime("%Y-%m-%d_%H-%M-%S")
    log_path = os.path.join(log_dir, "obo_transform_" + timestring + ".log")
    log_level = logging.INFO
    log_format = ("%(asctime)s [%(levelname)s]: %(message)s in %(pathname)s:%(lineno)d")

    root_logger_handler = logging.FileHandler(log_path)
    root_logger_handler.setFormatter(logging.Formatter(log_format))

    kg_obo_logger = logging.getLogger("kg-obo")
    kg_obo_logger.setLevel(log_level)
    kg_obo_logger.addHandler(root_logger_handler)

    kgx_logger = get_logger()
    kgx_logger.setLevel(log_level)
    kgx_logger.addHandler(root_logger_handler)

    # Check if there's already a run in progress (i.e., lock file exists)
    # This isn't an error so it does not trigger an exit
    if s3_test:
        if kg_obo.upload.mock_check_lock(bucket, lock_file_remote_path):
            print("Could not mock checking for lock file. Exiting...")
            return True
    else:
        if kg_obo.upload.check_lock(bucket, lock_file_remote_path):
            print("A kg-obo run appears to be in progress. Exiting...")
            return True

    # Now set the lockfile
    if s3_test:
        if not kg_obo.upload.mock_set_lock(bucket, lock_file_remote_path, unlock=False):
            print("Could not mock setting lock file. Exiting...")
            return False
    else:
        if not kg_obo.upload.set_lock(bucket, lock_file_remote_path, unlock=False):
            print("Could not set lock file on remote server. Exiting...")
            return False

    # Check on existence of tracking file
    if s3_test:
        if not kg_obo.upload.mock_check_tracking(bucket, tracking_file_remote_path):
            print("Could not mock checking tracking file. Exiting...")
            return False
    else:
        if not kg_obo.upload.check_tracking(bucket, tracking_file_remote_path):
            print("Cannot locate tracking file on remote storage. Exiting...")
            return False

    # If requested, refresh the root index.html
    if force_index_refresh and not s3_test:
        print(f"Refreshing root index on {bucket}")
        if kg_obo.upload.upload_index_files(bucket, remote_path, data_dir, data_dir, 
                                             update_root=True, refresh=True):
            kg_obo_logger.info(f"Refreshed root index at {remote_path}")
        else:
            kg_obo_logger.info(f"Failed to refresh root index at {remote_path}")

    # Get the OBO Foundry list YAML and process each
    yaml_onto_list_filtered = retrieve_obofoundry_yaml(skip=skip, get_only=get_only)

    successful_transforms = []
    errored_transforms = []
    failed_transforms = []
    all_completed_transforms = []

    if len(skip) >0:
      kg_obo_logger.info(f"Ignoring these OBOs: {skip}" )
    if save_local:
      kg_obo_logger.info("Will retain all downloaded files.")
    if s3_test:
      kg_obo_logger.info("Will test S3 upload instead of actually uploading.")

    for ontology in tqdm(yaml_onto_list_filtered, "processing ontologies"):
        ontology_name = ontology['id']
        print(f"{ontology_name}")
        kg_obo_logger.info("Loading " + ontology_name)

        # take base ontology if it exists, otherwise just use non-base
        url = kg_obo.obolibrary_utils.base_url_if_exists(ontology_name)
        print(url)

        # Set up local directories
        if not os.path.exists(data_dir):
            os.mkdir(data_dir)
        base_obo_path = os.path.join(data_dir, ontology_name)
        if not os.path.exists(base_obo_path):
            os.mkdir(base_obo_path)

        # Downloaded OBOs are still tempfiles as we don't intend to keep them
        with tempfile.NamedTemporaryFile(prefix=ontology_name) as tfile:

            success = True
            
            # Some OBOs are quite large, so we download selection first to check IRI/version
            if not download_ontology(url=url, file=tfile.name, logger=kg_obo_logger, 
                                     no_dl_progress=no_dl_progress, header_only=True):
                success = False
                kg_obo_logger.warning(f"Failed to load due to KeyError: {ontology_name}")
                failed_transforms.append(ontology_name)
                continue

            owl_iri, owl_version = get_owl_iri(tfile.name)
            kg_obo_logger.info(f"Current VersionIRI for {ontology_name}: {owl_iri}")
            print(f"Current VersionIRI for {ontology_name}: {owl_iri}")
            kg_obo_logger.info(f"Current version for {ontology_name}: {owl_version}")
            print(f"Current version for {ontology_name}: {owl_version}")

            # Check version here
            if transformed_obo_exists(ontology_name, owl_iri, s3_test, bucket):
                kg_obo_logger.info(f"Have already transformed {ontology_name}: {owl_iri}")
                print(f"Have already transformed {ontology_name}: {owl_iri} - skipping")
                all_completed_transforms.append(ontology_name)
                continue

            # Check for imports and skip this OBO if they're present
            # TODO: actually retrieve imports
            imports = imports_requested(tfile.name)
            if len(imports) > 0:
                fimports = ", ".join(imports)
                kg_obo_logger.info(f"Header for {ontology_name} requests these imports: {fimports}")
                kg_obo_logger.warning(f"Imports not currently supported. Skipping {ontology_name}.")
                print(f"Header for {ontology_name} requests these imports: {fimports}")
                print(f"Imports not currently supported. Skipping {ontology_name}.")
                failed_transforms.append(ontology_name)
                continue

            # If this version is new, download the whole OBO
            if not download_ontology(url=url, file=tfile.name, logger=kg_obo_logger, 
                                     no_dl_progress=no_dl_progress, header_only=False):
                success = False
                kg_obo_logger.warning(f"Failed to load due to KeyError: {ontology_name}")
                failed_transforms.append(ontology_name)
                continue

            versioned_obo_path = os.path.join(base_obo_path, owl_version)
            if not os.path.exists(versioned_obo_path):
                os.mkdir(versioned_obo_path)

            # Use kgx to transform, but save errors to log
            # Do separate transforms for different output formats
            success = True # for all transforms 
            errors = False # for all transforms
            all_success_and_errors = {}
            desired_output_formats = ['tsv', 'json']
            for output_format in desired_output_formats:
                kg_obo_logger.info(f"Transforming to {output_format}...")
                if output_format == 'tsv':
                    ontology_filename = f"{ontology_name}_kgx_tsv"
                else:
                    ontology_filename = f"{ontology_name}_kgx"
                this_success, this_errors, this_output_msg = kgx_transform(input_file=[tfile.name],
                                            input_format='owl',
                                            output_file=os.path.join(versioned_obo_path, ontology_filename),
                                            output_format=output_format,
                                            logger=kgx_logger)
                all_success_and_errors[output_format] = (this_success, this_errors)
                kg_obo_logger.info(this_output_msg)

            # Check results of all transforms
            for output_format in desired_output_formats:
                if not all_success_and_errors[output_format][0]:
                    success = False
                    break
            for output_format in desired_output_formats:
                if all_success_and_errors[output_format][1]:
                    errors = True
                    break

            # Check file size and fail/warn if nodes|edge file is empty
            for filename in os.listdir(versioned_obo_path):
              if os.stat(os.path.join(versioned_obo_path, filename)).st_size == 0:
                  kg_obo_logger.warning("Output is empty - something went wrong during transformation.")
                  success = False
              else:
                  kg_obo_logger.info(f"{filename} {os.stat(os.path.join(versioned_obo_path, filename)).st_size} bytes")

            if success and not errors:
                kg_obo_logger.info(f"Successfully completed transform of {ontology_name}")
                successful_transforms.append(ontology_name)
                all_completed_transforms.append(ontology_name)

            elif success and errors:
                kg_obo_logger.info(f"Completed transform of {ontology_name} with errors - see logs for details.")
                errored_transforms.append(ontology_name)
                all_completed_transforms.append(ontology_name)
            else:
                kg_obo_logger.warning(f"Failed to transform {ontology_name}")
                failed_transforms.append(ontology_name)

            if success:
                versioned_remote_path = os.path.join(remote_path,ontology_name,owl_version)
                if not s3_test:
                    track_obo_version(ontology_name, owl_iri, owl_version, bucket)
                    # Update indexes for this version and OBO only
                    if kg_obo.upload.upload_index_files(bucket, remote_path, versioned_obo_path, data_dir, update_root=False):
                        kg_obo_logger.info(f"Created index for {ontology_name}")
                    else:
                        kg_obo_logger.info(f"Failed to create index for {ontology_name}")
                    
                    # Upload the most recently transformed version only
                    kg_obo_logger.info("Uploading...")
                    kg_obo.upload.upload_dir_to_s3(versioned_obo_path,bucket,versioned_remote_path,make_public=True)
                else:
                    kg_obo.upload.mock_upload_dir_to_s3(versioned_obo_path,bucket,versioned_remote_path,make_public=True)

            # Clean up any incomplete transform leftovers
            if not success:
                if delete_path(base_obo_path):
                    kg_obo_logger.info(f"Removed incomplete transform files for {ontology_name}.")
                else:
                    kg_obo_logger.warning(f"Incomplete version of {ontology_name} may be present.")

    kg_obo_logger.info(f"Successfully transformed {len(successful_transforms)} without errors: {successful_transforms}")

    if len(errored_transforms) > 0:
        kg_obo_logger.info(f"Successfully transformed {len(errored_transforms)} with errors: {errored_transforms}")

    if len(failed_transforms) > 0:
        kg_obo_logger.info(f"Failed to transform {len(failed_transforms)}: {failed_transforms}")

    if len(all_completed_transforms) > 0:
        kg_obo_logger.info(f"All available transforms, including old versions ({len(all_completed_transforms)}): "
                            f"{all_completed_transforms}")

    if not s3_test:
        # Update the root index
        if kg_obo.upload.upload_index_files(bucket, remote_path, data_dir, data_dir, update_root=True):
            kg_obo_logger.info(f"Created root index at {remote_path}")
        else:
            kg_obo_logger.info(f"Failed to create root index at {remote_path}")

    # Remove all local data files
    # TODO: check to ensure no empty folders remain, whether we're using save_local or not
    if not save_local:
        if delete_path(data_dir, omit = [track_file_local_path]):
            kg_obo_logger.info(f"Removed local data from {data_dir}.")
        else:
            kg_obo_logger.warning(f"Local data not deleted from {data_dir}.")

    # Now un-set the lockfile
    if s3_test:
        if not kg_obo.upload.mock_set_lock(bucket,lock_file_remote_path,unlock=True):
            sys.exit("Could not mock setting lock file. Exiting...")
    else:
        if not kg_obo.upload.set_lock(bucket,lock_file_remote_path,unlock=True):
            sys.exit("Could not set lock file on remote server. Exiting...")

    return True

