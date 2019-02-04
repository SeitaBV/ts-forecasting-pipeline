from typing import List, Optional, Tuple, Type, Union, Dict, Any
from datetime import datetime, timedelta, tzinfo
from pprint import pformat
import json
import warnings
import logging
import inspect

import pytz
import dateutil.parser
import numpy as np
import pandas as pd
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Query
from sqlalchemy.dialects import postgresql

from ts_forecasting_pipeline.utils.debug_utils import render_query
from ts_forecasting_pipeline.utils.time_utils import (
    tz_aware_utc_now,
    timedelta_to_pandas_freq_str,
    timedelta_fits_into,
)
from ts_forecasting_pipeline.exceptions import IncompatibleModelSpecs
from ts_forecasting_pipeline.transforming import Transformation

"""
Specs for the context of your model and how to treat your model data.
"""

DEFAULT_RATIO_TRAINING_TESTING_DATA = 2 / 3
DEFAULT_REMODELING_FREQUENCY = timedelta(days=1)

np.seterr(all="warn")
warnings.filterwarnings("error", message="invalid value encountered in power")

logger = logging.getLogger(__name__)


class SeriesSpecs(object):
    """Describes a time series (e.g. a pandas Series).
    In essence, a column in the regression frame, filled with numbers.

    Using this base class, the column will be filled with NaN values.

    If you have data to be loaded in automatically, you should be using one of the subclasses, which allow to describe
    or pass in an actual data source to be loaded.

    When dealing with columns, our code should usually refer to this superclass so it does not need to care
    which kind of data source it is dealing with.
    """

    # The name in the resulting feature frame, and possibly in the saved model specs (named by outcome var)
    name: str
    # The name of the data column in the data source. If None, the name will be tried.
    column: Optional[str]
    # timezone of the data - useful when de-serializing (e.g. pandas serialises to UTC)
    original_tz: tzinfo
    # Custom transformation to perform on the outcome data. Called after relevant SeriesSpecs were resolved.
    transformation: Optional[Transformation]
    # Custom resampling parameters. All parameters apply to pd.resample, only "aggregation" is the name
    # of the aggregation function to be called of the resulting resampler
    resampling_config: Dict[str, Any]

    def __init__(
        self,
        name: str,
        original_tz: Optional[tzinfo] = None,
        transformation: Optional[Transformation] = None,
        resampling_config: Dict[str, Any] = None,
    ):
        self.name = name
        self.original_tz = original_tz
        self.transformation = transformation
        self.resampling_config = resampling_config
        self.__series_type__ = self.__class__.__name__

    def as_dict(self):
        return vars(self)

    def _load_series(self) -> pd.Series:
        """Subclasses overwrite this function to get the raw data"""
        return pd.Series()

    def load_series(
        self, expected_frequency: timedelta
    ) -> pd.Series:
        """Load the series data, check compatibility of series data with model specs and transform, if needed.

           The actual implementation how to load is deferred to _load_series. Overwrite that for new subclasses.

           This function resamples data if the frequency is not compatible.
           It is possible to customise resampling (without that, we aggregate means after default resampling.
           Pass in a `resampling_config` to the class with an aggregation method name and
           kw params to pass into `resample`. For example:

           `resampling_config={"closed": "left", "aggregation": "sum"}`
        """
        data = self._load_series()

        # check if data has a DateTimeIndex
        if not isinstance(data.index, pd.DatetimeIndex):
            raise IncompatibleModelSpecs(
                "Loaded series has no DatetimeIndex, but %s" % type(data.index).__name__
            )

        # make sure we have a time zone (default to UTC), save original time zone
        if data.index.tzinfo is None:
            self.original_tz = pytz.utc
            data.index = data.index.tz_localize(self.original_tz)
        else:
            self.original_tz = data.index.tzinfo

        # Raise error if data is empty or contains nan values
        if data.empty:
            raise ValueError(
                "No values found in requested %s data. It's no use to continue I'm afraid."
            )
        if data.isnull().values.any():
            raise ValueError(
                "Nan values found in the requested %s data. It's no use to continue I'm afraid."
            )

        # check if time series frequency is okay, if not then resample
        if data.index.freq.freqstr != timedelta_to_pandas_freq_str(expected_frequency):
            data = self.resample_data(data, expected_frequency)

        if self.transformation is not None:
            data = pd.Series(
                index=data.index, data=self.transformation.transform(data.values)
            )

        return data

    def resample_data(self, data, expected_frequency) -> pd.Series:
        if self.resampling_config is None:
            data = data.resample(
                timedelta_to_pandas_freq_str(expected_frequency)
            ).mean()
        else:
            data_resampler = data.resample(
                timedelta_to_pandas_freq_str(expected_frequency),
                **{k: v for k, v in self.resampling_config.items() if k != "aggregation"}
            )
            if "aggregation" not in self.resampling_config:
                data = data_resampler.mean()
            else:
                for agg_name, agg_method in inspect.getmembers(
                    data_resampler, inspect.ismethod
                ):
                    if self.resampling_config["aggregation"] == agg_name:
                        data = agg_method()
                        break
                else:
                    raise IncompatibleModelSpecs(
                        "Cannot find resampling aggregation %s on %s"
                        % (self.resampling_config["aggregation"], data_resampler)
                    )
        return data

    def __repr__(self):
        return "%s: <%s>" % (self.__class__.__name__, self.as_dict())


class ObjectSeriesSpecs(SeriesSpecs):
    """
    Spec for a pd.Series object that is being passed in and is stored directly in the specs.
    """

    data: pd.Series

    def __init__(
        self,
        data: pd.Series,
        name: str,
        original_tz: Optional[tzinfo] = None,
        transformation: Optional[Transformation] = None,
        resampling_config: Dict[str, Any] = None,
    ):
        super().__init__(name, original_tz, transformation, resampling_config)
        if not isinstance(data.index, pd.DatetimeIndex):
            raise IncompatibleModelSpecs(
                "Please provide a DatetimeIndex. Only found %s."
                % type(data.index).__name__
            )
        self.data = data

    def _load_series(self) -> pd.Series:
        return self.data


class CSVFileSeriesSpecs(SeriesSpecs):
    # TODO: Make this
    pass


class DFFileSeriesSpecs(SeriesSpecs):
    """
    Spec for a pandas DataFrame source.
    This class holds the filename, from which we unpickle the data frame, then read the column.
    """

    file_path: str
    column: str

    def __init__(
        self,
        file_path: str,
        name: str,
        column: str = None,
        original_tz: Optional[tzinfo] = None,
        transformation: Transformation = None,
        resampling_config: Dict[str, Any] = None,
    ):
        super().__init__(name, original_tz, transformation, resampling_config)
        self.file_path = file_path
        self.column = column

    def _load_series(self) -> pd.Series:
        df: pd.DataFrame = pd.read_pickle(self.file_path)

        return df[self.column]


class DBSeriesSpecs(SeriesSpecs):

    """Define how to query a database for time series values.
    This works via a SQLAlchemy query.
    This query should return the needed information for the forecasting pipeline:
    A "datetime" column (which will be set as index of the series) and a "value" column.
    """

    db: Engine
    query: Query

    def __init__(
        self,
        db_engine: Engine,
        query: Query,
        name: str = "value",
        original_tz: Optional[tzinfo] = pytz.utc,  # postgres stores naive datetimes
        transformation: Transformation = None,
            resampling_config: Dict[str, Any] = None,
    ):
        super().__init__(name, original_tz, transformation, resampling_config)
        self.db_engine = db_engine
        self.query = query

    def _load_series(self) -> pd.Series:
        logger.info(
            "Reading %s data from database"
            % self.query.column_descriptions[0]["entity"].__tablename__
        )

        df = pd.DataFrame(
            self.query.all(),
            columns=[col["name"] for col in self.query.column_descriptions],
        )
        df["datetime"] = pd.to_datetime(df["datetime"], utc=True)

        # Raise error if data is empty or contains nan values. Here, other than in load_series, we can show the query.
        if df.empty:
            raise ValueError(
                "No values found in database for the requested %s data. It's no use to continue I'm afraid."
                " Here's a print-out of the database query:\n\n%s\n\n"
                % (
                    self.query.column_descriptions[0]["entity"].__tablename__,
                    render_query(self.query.statement, dialect=postgresql.dialect()),
                )
            )
        if df.isnull().values.any():
            raise ValueError(
                "Nan values found in database for the requested %s data. It's no use to continue I'm afraid."
                " Here's a print-out of the database query:\n\n%s\n\n"
                % (
                    self.query.column_descriptions[0]["entity"].__tablename__,
                    render_query(self.query.statement, dialect=postgresql.dialect()),
                )
            )

        # TODO: this is a post-processing function - move to func store maybe
        # Keep the most recent observation
        series = (
            df.sort_values(by=["horizon"], ascending=True)
            .drop_duplicates(subset=["datetime"], keep="first")
            .sort_values(by=["datetime"])
        )
        series.set_index("datetime", drop=True, inplace=True)

        return series["value"]


class ModelSpecs(object):
    """Describes a model and how it was trained.
    """

    outcome_var: SeriesSpecs
    model_type: Type  # e.g. statsmodels.api.OLS, sklearn.linear_model.LinearRegression, ...
    model_params: dict
    frequency: timedelta
    horizon: timedelta
    lags: List[int]
    regressors: List[SeriesSpecs]
    # Start of training data set
    start_of_training: datetime
    # End of testing data set
    end_of_testing: datetime
    # This determines the cutoff point between training and testing data
    ratio_training_test_data: float
    # time this model was created, defaults to UTC now
    creation_time: datetime
    model_filename: str
    remodel_frequency: timedelta

    def __init__(
        self,
        outcome_var: Union[str, SeriesSpecs, pd.Series],
        model: Union[
            Type, Tuple[Type, dict]
        ],  # Model class and optionally initialization parameters
        start_of_training: Union[str, datetime],
        end_of_testing: Union[str, datetime],
        frequency: timedelta,
        horizon: timedelta,
        lags: List[int] = None,
        regressors: Union[List[str], List[SeriesSpecs], List[pd.Series]] = None,
        ratio_training_testing_data=DEFAULT_RATIO_TRAINING_TESTING_DATA,
        remodel_frequency: Union[str, timedelta] = DEFAULT_REMODELING_FREQUENCY,
        model_filename: str = None,
        creation_time: Union[str, datetime] = None,
    ):
        """Create a ModelSpecs instance. Accepts all parameters as string (besides transform - TODO) for
         deserialization support (JSON strings for all parameters which are not natively JSON-parseable,)"""
        self.outcome_var = parse_series_specs(outcome_var, "y")
        self.model_type = model[0] if isinstance(model, tuple) else model
        self.model_params = model[1] if isinstance(model, tuple) else {}
        self.frequency = frequency
        self.horizon = horizon
        self.lags = lags
        if self.lags is None:
            self.lags = []
        if regressors is None:
            self.regressors = []
        else:
            self.regressors = [
                parse_series_specs(r, "Regressor%d" % (regressors.index(r) + 1))
                for r in regressors
            ]
        if isinstance(start_of_training, str):
            self.start_of_training = dateutil.parser.parse(start_of_training)
        else:
            self.start_of_training = start_of_training
        if isinstance(end_of_testing, str):
            self.end_of_testing = dateutil.parser.parse(end_of_testing)
        else:
            self.end_of_testing = end_of_testing
        self.ratio_training_testing_data = ratio_training_testing_data
        # check if training + testing period is compatible with frequency
        if not timedelta_fits_into(
            self.frequency, self.end_of_testing - self.start_of_training
        ):
            raise IncompatibleModelSpecs(
                "Training & testing period (%s to %s) does not fit with frequency (%s)"
                % (self.start_of_training, self.end_of_testing, self.frequency)
            )

        if isinstance(creation_time, str):
            self.creation_time = dateutil.parser.parse(creation_time)
        elif creation_time is None:
            self.creation_time = tz_aware_utc_now()
        else:
            self.creation_time = creation_time
        self.model_filename = model_filename
        if isinstance(remodel_frequency, str):
            self.remodel_frequency = timedelta(
                days=int(remodel_frequency) / 60 / 60 / 24
            )
        else:
            self.remodel_frequency = remodel_frequency

    def as_dict(self):
        return vars(self)

    def __repr__(self):
        return "ModelSpecs: <%s>" % pformat(vars(self))


def parse_series_specs(
    specs: Union[str, SeriesSpecs, pd.Series], name: str = None
) -> SeriesSpecs:
    if isinstance(specs, str):
        return load_series_specs_from_json(specs)
    elif isinstance(specs, pd.Series):
        return ObjectSeriesSpecs(specs, name)
    else:
        return specs


def load_series_specs_from_json(s: str) -> SeriesSpecs:
    json_repr = json.loads(s)
    series_class = globals()[json_repr["__series_type__"]]
    if series_class == ObjectSeriesSpecs:
        # load pd.Series from string, will be UTC-indexed, so apply original_tz
        json_repr["data"] = pd.read_json(
            json_repr["data"], typ="series", convert_dates=True
        )
        json_repr["data"].index = json_repr["data"].index.tz_localize(
            json_repr["original_tz"]
        )
    return series_class(
        **{k: v for k, v in json_repr.items() if not k.startswith("__")}
    )
