# -*- coding: utf-8 -*-

import os

import copy
from enum import Enum
from six import string_types

import logging

from ruamel.yaml import YAML

from ..data_context.util import NormalizedDataAssetName

logger = logging.getLogger(__name__)
yaml = YAML()
yaml.default_flow_style = False


class ReaderMethods(Enum):
    CSV = 1
    csv = 1
    parquet = 2
    excel = 3
    xls = 3
    xlsx = 3
    JSON = 4
    json = 4


class Datasource(object):
    """Datasources are responsible for connecting to data infrastructure. Each Datasource is a source 
    of materialized data, such as a SQL database, S3 bucket, or local file directory.

    Each Datasource also provides access to Great Expectations data assets that are connected to
    a specific compute environment, such as a SQL database, a Spark cluster, or a local in-memory
    Pandas Dataframe.

    To bridge the gap between those worlds, Datasources interact closely with *generators* which
    are aware of a source of data and can produce produce identifying information, called 
    "batch_kwargs" that datasources can use to get individual batches of data. They add flexibility 
    in how to obtain data such as with time-based partitioning, downsampling, or other techniques
    appropriate for the datasource.

    For example, a generator could produce a SQL query that logically represents "rows in the Events
    table with a timestamp on February 7, 2012," which a SqlAlchemyDatasource could use to materialize
    a SqlAlchemyDataset corresponding to that batch of data and ready for validation. 

    Since opinionated DAG managers such as airflow, dbt, prefect.io, dagster can also act as datasources
    and/or generators for a more generic datasource.
    """

    @classmethod
    def from_configuration(cls, **kwargs):
        return cls(**kwargs)

    def __init__(self, name, type_, data_context=None, generators=None):
        self._data_context = data_context
        self._name = name
        self._generators = {}
        if generators is None:
            generators = {}
        self._datasource_config = {
            "type": type_,
            "generators": generators
        }

        # extra_config = self._load_datasource_config()
        # self._datasource_config.update(extra_config)

    @property
    def data_context(self):
        return self._data_context

    @property
    def name(self):
        return self._name

    def _build_generators(self):
        for generator in self._datasource_config["generators"].keys():
            self.get_generator(generator)

    # def _load_datasource_config(self):
    #     # For now, just use the data context config
    #     return {}
    #     # if self._data_context is None:
    #     #     # Setup is done; no additional config to read
    #     #     return {}
    #     # try:
    #     #     config_path = os.path.join(self._data_context.root_directory,
    #                                      "datasources", self._name, "config.yml")
    #     #     with open(config_path, "r") as data:
    #     #         extra_config = yaml.load(data) or {}
    #     #     logger.info("Loading config from %s" % str(config_path))
    #     #     return extra_config
    #     # except FileNotFoundError:
    #     #     logger.debug("No additional config file found.")
    #     #     return {}

    def get_credentials(self, profile_name):
        if self._data_context is not None:
            return self._data_context.get_profile_credentials(profile_name)
        return {}

    def get_config(self):
        if self._data_context is not None:
            self.save_config()
        return self._datasource_config

    def save_config(self):
        """Save the datasource config.

        If there is no attached DataContext, a datasource will save its config in the current directory
        in a file called "great_expectations.yml

        Returns:
            None
        """
        if self._data_context is not None:
            self._data_context._save_project_config()
        else:
            config_filepath = "great_expectations.yml"
            with open(config_filepath, 'w') as config_file:
                yaml.dump(self._datasource_config, config_file)

        # if self._data_context is not None:
        #     base_config = copy.deepcopy(self._datasource_config)
        #     if "config_file" in base_config:
        #         config_filepath = os.path.join(self._data_context.root_directory,
        #                                        base_config.pop["config_file"])
        #     else:
        #         config_filepath = os.path.join(self._data_context.root_directory,
        #                                        "datasources", self._name, "config.yml")
        # else:
        #     logger.warning("Unable to save config with no data context attached.")

        # safe_mmkdir(os.path.dirname(config_filepath), exist_ok=True)
        # with open(config_filepath, "w") as data_file:
        #     yaml.safe_dump(self._datasource_config, data_file)

    def add_generator(self, name, type_, **kwargs):
        """Add a generator to the datasource.

        The generator type_ must be one of the recognized types for the datasource.

        Args:
            name (str): the name of the new generator to add
            type_ (str): the type of the new generator to add
            kwargs: additional keyword arguments will be passed directly to the new generator's constructor

        Returns:
             generator (Generator)
        """
        data_asset_generator_class = self._get_generator_class(type_)
        generator = data_asset_generator_class(name=name, datasource=self, **kwargs)
        self._generators[name] = generator
        if "generators" not in self._datasource_config:
            self._datasource_config["generators"] = {}
        self._datasource_config["generators"][name] = generator.get_config()
        if self._data_context is not None:
            self.save_config()
        return generator

    def get_generator(self, generator_name="default"):
        """Get the (named) generator from a datasource)

        Args:
            generator_name (str): name of generator (default value is 'default')

        Returns:
            generator (Generator)
        """     
        if generator_name in self._generators:
            return self._generators[generator_name]
        elif generator_name in self._datasource_config["generators"]:
            generator_config = copy.deepcopy(self._datasource_config["generators"][generator_name])
        elif len(self._datasource_config["generators"]) == 1:
            # If there's only one generator, we will use it by default
            generator_name = list(self._datasource_config["generators"])[0]
            generator_config = copy.deepcopy(self._datasource_config["generators"][generator_name])
        else:
            raise ValueError(
                "Unable to load generator %s -- no configuration found or invalid configuration." % generator_name
            )
        type_ = generator_config.pop("type")
        generator_class = self._get_generator_class(type_)
        generator = generator_class(name=generator_name, datasource=self, **generator_config)
        self._generators[generator_name] = generator
        return generator

    def list_generators(self):
        """List currently-configured generators for this datasource.

        Returns:
            List(dict): each dictionary includes "name" and "type" keys
        """

        return [{"name": key, "type": value["type"]} for key, value in self._datasource_config["generators"].items()]

    def get_batch(self, data_asset_name, expectation_suite_name="default", batch_kwargs=None, **kwargs):
        if isinstance(data_asset_name, NormalizedDataAssetName):  # this richer type can include more metadata
            generator_name = data_asset_name.generator
            generator_asset = data_asset_name.generator_asset
            if self._data_context is not None:
                expectation_suite = self._data_context.get_expectation_suite(
                    data_asset_name,
                    expectation_suite_name
                )
            else:
                expectation_suite = None
                # If data_context is not set, we cannot definitely use a fully normalized data_asset reference.
                # This would mean someone got a normalized name without a data context which is unusual
                logger.warning(
                    "Using NormalizedDataAssetName type without a data_context could result in unexpected behavior: "
                    "using '/' as a default delimiter."
                )
        else:
            generators = [generator["name"] for generator in self.list_generators()]
            if len(generators) == 1:
                generator_name = generators[0]
            elif "default" in generators:
                generator_name = "default"

            generator_asset = data_asset_name
            expectation_suite = None
            if self._data_context is not None:
                logger.warning(
                    "Requesting a data_asset without a normalized data_asset_name; expectation_suite will not be set"
                )

        if batch_kwargs is None:
            generator = self.get_generator(generator_name)
            if generator is not None:
                batch_kwargs = generator.yield_batch_kwargs(generator_asset)
            else:
                raise ValueError("No generator or batch_kwargs available to provide a dataset.")
        elif not isinstance(batch_kwargs, dict):
            batch_kwargs = self.build_batch_kwargs(batch_kwargs)

        return self._get_data_asset(batch_kwargs, expectation_suite, **kwargs)

    def _get_data_asset(self, batch_kwargs, expectation_suite, **kwargs):
        raise NotImplementedError

    def _get_generator_class(self, type_):
        raise NotImplementedError

    def get_available_data_asset_names(self, generator_names=None):
        available_data_asset_names = {}
        if generator_names is None:
            generator_names = [generator["name"] for generator in self.list_generators()]
        elif isinstance(generator_names, string_types):
            generator_names = [generator_names]

        for generator_name in generator_names:
            generator = self.get_generator(generator_name)
            available_data_asset_names[generator_name] = generator.get_available_data_asset_names()
        return available_data_asset_names

    def build_batch_kwargs(self, *args, **kwargs):
        raise NotImplementedError

    def get_data_context(self):
        return self._data_context

    @staticmethod
    def _guess_reader_method_from_path(path):
        if path.endswith(".csv") or path.endswith(".tsv"):
            return ReaderMethods.CSV
        elif path.endswith(".parquet"):
            return ReaderMethods.parquet
        elif path.endswith(".xlsx") or path.endswith(".xls"):
            return ReaderMethods.excel
        elif path.endswith(".json"):
            return ReaderMethods.JSON
        else:
            return None
